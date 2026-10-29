---
name: hgrill
description: Automated grill chain on herdr — one tab per issue runs grill or product-grill on each configured engine in turn, accepting every recommendation, then reconciles disputed calls and posts the biggest product calls and assumptions to the top of the issue for a human check.
disable-model-invocation: true
argument-hint: '<issue> [<issue> ...] | next <k> [--kind grill|product-grill]'
---

# /hgrill — automated multi-engine grill chain

Usage:

- `/hgrill <n> [<m> ...] [--kind grill|product-grill]` — chain each named issue.
- `/hgrill next <k> [--kind grill|product-grill]` — chain the top `k` issues of the grill queue.
- `/hgrill run <n> --kind <kind>` — internal: the driver inside one issue's tab. The dispatcher sends it.
- `/hgrill run <n> --kind <kind> --from correction --owner <file>` — internal: the owner's sign-off rejected or amended some calls; `<file>` holds the corrections. Run [Correction pass](#correction-pass) with those corrections as its input, introduced as "The issue owner reviewed the assumptions block and made these corrections; they are decisions, not proposals", then redo [Assumptions](#assumptions) and post with the hold still on. The pass applies the owner's corrections and works out their consequences; it does not re-argue them.

Two roles:

- **Dispatcher** (the session the user invoked, or `/horch`): picks issues, opens one tab per issue, keeps at most **3** chain tabs running, and starts the next queued issue when a tab finishes.
- **Driver** (the session in each tab's first pane): runs that issue's chain. It answers as the issue owner, never runs a grill itself, and never counts as one of the passes.

Mechanics: [`herdr.md`](../../references/herdr.md). Design: loomantix/activeloom#422.

## Hard stop

Touch only the chained issue and its own children: the children an earlier pass or this chain's handbacks create.

## Standing authorization

Invoking this skill, or an instruction that schedules it under `/horch`, is the user's standing approval for each chained issue to:

- accept every recommendation in every round;
- confirm each summary and handback preview, including closes and split children;
- post decision comments and let refinement apply its plan after every pass;
- reopen an issue that a later pass reaches a build outcome on;
- edit the top of the issue body with the assumptions block, and add the hold label.

The driver relays this approval to the engine sessions; that relay is the design. Nothing else is authorized: no other issue, no code, branches, PRs or merges, and no label change beyond adding the hold and removing the configured verify-first label.

## Settings

- `C` = `.claude/skills/hreview/scripts/herd-config.py`; `P` = `.claude/skills/review-setup/scripts/review-profile.py`.
- Engines and order: `grill.engines` from `python3 -I $C repo`, minus any engine `python3 -I $P show` marks unavailable. Fewer than two left is a block.
- Model and effort per engine: `python3 -I $P launch-args --engine <e> --repo <repo>`; extra arguments from `python3 -I $C user-args --engine <e>`.
- Interviewee: `grill.persona`, or `the issue owner` when it is null.
- Hold label: `grill.hold_label`. Verify-first label: `grill.verify_first_label` (skip that step when null).

A grill is not a review-chain pass: no ledger, no launcher, interactive sessions on every engine.

## Dispatch

1. `test "${HERDR_ENV:-}" = 1`, or stop. `repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)`. `python3 -I $C repo` failing is a block. Then the permissions preflight and its Claude auto-mode check (herdr.md) for this session's engine and every grill engine.
2. Pick issues.
   - Named: use them as given.
   - `next <k>`: `python3 .claude/skills/backlog-refinement/scripts/candidates.py --grill --json`, highest priority first, filtered by `--kind` if given.
   - Skip, and say why, any issue that is closed, assigned, already carries the hold label (it waits on the user), or has a held lock (step 4).
3. Kind per issue: `--kind` if given; else `needs: product-grill` → `product-grill`, `needs: grill` → `grill`; else `grill`. The kind is fixed for the whole chain, so every engine stress-tests the same interview.
4. Lock: `python3 -I $C lock --repo $repo --skill hgrill --key issue-<n>`. Exit 3 means the issue is chained or was left mid-chain: skip it and report the lock path. The driver releases the lock when its chain ends cleanly; a block leaves it for the user.
5. Open a `G<n>` tab with the primary checkout as `--cwd` (grills read the repo and write nothing to it), start the driver there as `g<n>-driver` on this session's engine (`claude`, with its profile model and effort), and prompt it `run <n> --kind <kind>` through this skill, addressed the way herdr.md's Engines table gives for that engine. At most 3 chain tabs at once.
6. Watch each tab's **label**, not its driver's state: a driver ends its turn whenever it waits in the background, so `idle` or `done` mid-chain is normal. Poll the tab list until the label is `G<n> ✓` or `G<n> ⚠`, then start the next queued issue. Report a short table at the end: issue, kind, outcome per pass, final state, tab.

## The driver's chain

State: `python3 -I $C paths --repo <repo> --skill hgrill --key issue-<n>`; `state.md` there holds identifiers only (pane IDs, pass outcomes, comment URLs, timestamps).

### Layout

Split one row below the driver's pane with one pane per engine, titled by engine. A running pass is `<engine> ▶`, a landed one `<engine> ✓ <outcome>`.

### Each pass (strictly in order)

1. **Start** the engine in its pane, named `g<n>-<engine>`. Before the first pass, record `git -C <primary checkout> status --porcelain` and `git worktree list` in `state.md` as the debris baseline.
2. **Prompt.** First line: the `<kind>` skill for issue `#<n>`, addressed the way the engine expects. Then:

   ```text
   Automated grill chain for <repo>#<n>, pass <k> of <total> (<engine>), kind <kind>. An orchestrator answers for the issue owner under their standing approval.
   - Interviewee: <persona>. Do not save an interviewee profile.
   - Earlier passes in this chain: <decision comment URLs, or "none">. Treat them as proposals to stress-test, not settled decisions. A newer decision does not settle the agenda; reopen any call you disagree with and put it in a round.
   - Children an earlier pass created: <issue numbers, or "none">. If you change a decision they carry, your handback updates their bodies too, so no child keeps a superseded spec.
   - Existing excluded children use refinement's explicit existing_children/handback_parent plan, not a ready verdict or duplicate split. Keep the original split plan/private child recovery and pass --existing-child-plan. Preview current body hashes and full replacements; confirm this handback even in auto mode. Missing proof, a pending parent interview, assignment or drift blocks this pass without altering its lock or starting another engine.
   - End every message that waits for an answer with exactly one final line:
     HGRILL-WAIT: round <r>   (a question round)
     HGRILL-WAIT: summary     (the summary, awaiting confirmation)
     HGRILL-WAIT: handback    (the handback preview, awaiting approval)
     HGRILL-WAIT: other       (anything else)
   - After the handback is applied, end with one line: HGRILL-DONE: outcome=<outcome> next=<next> comment=<decision comment URL>. If it cannot complete, end with: HGRILL-FAILED: <reason>.
   - Work another issue needs (a constraint, a correction, a follow-up) goes under an `### Owed to other issues` heading in your decision comment, one line each with the issue number. Do not edit those issues.
   - Touch only #<n>, the children listed above, and children your handback creates. No code, branches, worktrees, or PRs; read the repo where it is. Do not launch another engine.
   ```

3. **Answer** each time the session settles (`herdr agent wait g<n>-<engine> --until idle --until done --until blocked --timeout 600000`). Read the last ~120 lines and act on the final sentinel line:
   - `round` → `Accept all recommendations as given. Continue to the next round.`
   - `summary`, the first time this pass → `Before I confirm: what are the most controversial calls made in this session? Give a recommendation for each in the same question format, then show the revised summary.`
   - `round` after that → accept all, as above.
   - `summary` after the controversy question → `Confirmed: the understanding is shared. Proceed to the record and handback.`
   - `handback` → `Approved as previewed, including any close and any split children.`
   - `other` → answer it if this skill or the grill skill's contract decides it; otherwise it is a block.
   - No sentinel → prompt `End your last message with the HGRILL line the chain asked for.` Twice with no sentinel is a block.
   - Never pick an answer that drops scope the session recommended keeping, or that edits an issue other than #<n> and this chain's children.
4. **Stalls and crashes** per herdr.md. The recovery line: `An earlier session for this pass exited. Read the issue's comments first and do not re-post a decision comment.`
5. **Landed** only when both hold:
   - the session printed `HGRILL-DONE`;
   - `gh issue view <n> --json comments,state,labels` shows a new comment carrying `grill-decision kind: <kind>`, posted after this pass started.

   Record the outcome, next, comment URL, issue state and labels in `state.md`. `HGRILL-FAILED`, or a done line with no matching comment, is a block.

6. **Reopen** only to overturn a don't-build: if an earlier pass in this chain closed the issue as `not planned` (outcome `dont-build`) and this pass reaches a build outcome, `gh issue reopen <n>` and say in one comment that pass <k> (<engine>) reached `<outcome>`, linking its decision. A close the handback made for any other reason stands.
7. **Debris.** Compare `git status --porcelain` and `git worktree list` with the baseline. Remove new `__pycache__` directories. Anything else new is not yours to delete: note it in `state.md` and the final report.
8. End the session, retitle the pane, notify `<engine>: <outcome>`, and start the next pass.

A closed issue does not end the chain: later passes still grill it and may overturn a don't-build.

### Reconcile

After the last pass, the driver does this itself. Accepting every recommendation means the last pass wins every disagreement by position, not merit; this step re-decides those.

1. Read, fresh: the issue body, labels and state, every pass's decision comment, and what each handback changed (labels, body rewrites, closes, children).
2. List the **disputes**: every call where a later pass reversed, widened or narrowed an earlier one. Agreed calls are not reopened.
3. Sort each into technical or product (the product-call test in Assumptions step 2).
4. **Technical dispute** — re-decide it on its merits. If it turns on a checkable fact, check it first under Assumptions step 3's rules. Which engine took a position, and whether it came first or last, count for nothing; when the reasons are equal, prefer the position that keeps more safety margin and is easier to reverse. Record `kept <position>` or `changed to <position>`, with a one-line reason.
5. **Product dispute** — do not decide it. Record the options, the one currently applied, and your recommendation with its reason, judged as if none were applied. Give the strongest case for the option you do not recommend in one line. The owner chooses at sign-off.
6. Collect every `Owed to other issues` line from the decisions and from step 4, deduplicated.
7. If no dispute exists, skip to Assumptions. Otherwise post one decision comment, `## Reconcile — disputed calls (<yyyy-mm-dd>)`, listing each dispute and its result, ending with the marker (same kind; outcome and next as the last pass's unless a changed call changes them). If any technical call changed, run refinement's handback with it (`assess <n> --decision <comment file>`, then apply), confirming under the standing authorization. A product dispute changes nothing on GitHub until the owner picks.

### Assumptions

After reconcile, the driver does this itself.

1. Read, fresh: the issue body, labels and state, every decision comment, and what each handback changed.
2. Answer two questions separately, drafting every candidate, most consequential first:
   - **What were the biggest product calls made in these passes?** A product call decides what users get or don't get: who it is for, what is in or out of scope, what a user can do where, what they see, what is deferred. It is the owner's call however a pass framed it, including product calls made inside a technical `grill`. No lookup settles one; it goes to the human.
   - **What were the biggest assumptions made in these passes?** Beliefs about facts the decisions rest on: how the code, a vendor, a policy or the world behaves.

   A call can be both. List it once, as a product call, and name the belief in its evidence line.

3. **Check the easy assumptions before posting.** For each candidate, ask whether a read-only lookup settles it now: the repo's code, config or docs; git history; issue and PR threads; a vendor's public documentation; or a read-only query the repository's agent guide documents. Spend a few tool calls on each, not a research project. Never call a paid or mutating API, write to any environment, or put customer data in scratch.
   - **Confirmed** → move it to the checked list with its source.
   - **Refuted** → a decision rests on a false premise. Note the false belief, the fact with its source, and the decision it affects, and go to [Correction pass](#correction-pass) before posting.
   - **Not checkable now** → it stays an assumption.

   Keep at most 5 product calls and 7 assumptions. Mark an entry ⚠ only when it was a dispute.
   - Product call: the call in one sentence, from the user's side; which passes made it; its **evidence** (a source, or `none — the passes' judgment`); and what users get instead if the owner reverses it. A ⚠ product call lists its options, marks the one applied, and gives the reconcile recommendation.
   - Assumption: one sentence, which passes made or relied on it, and what changes if it is wrong. A ⚠ technical call names the reconcile result and its reason.

   Keep the whole block under 500 words.

4. Prepend it to the body, re-reading the body right before the edit. Replace an existing block between the markers; never stack a second one. Leave out empty sections:

   ```markdown
   <!-- hgrill-assumptions:start -->

   > [!IMPORTANT]
   > **Automated grill — needs a human check.** <engines with model and effort> grilled this issue on <yyyy-mm-dd> and accepted every recommendation. Check the product calls first: they are yours to make, and the passes made them for you. Remove `<hold label>` once you have checked this block.

   **Refuted and not resolved — a decision still rests on this:**

   - ❌ **<what the passes assumed>** is false: <fact> (<source>). Affects: <decision>.

   **Product calls — yours to confirm:**

   1. ⚠ **<the question, as the user experiences it>** — the passes disagreed. Options: (a) <option> — <passes>; (b) <option> — <passes>. Applied now: (<x>). Recommended: (<y>), because <reason>. Evidence: <source, or none — the passes' judgment>.
   2. **<call, as the user experiences it>** — <passes>. Evidence: <source, or none — the passes' judgment>. If reversed: <what users get instead>.

   **Assumptions:**

   1. **<assumption>** — <passes>. If wrong: <consequence>.
   2. ⚠ **<assumption behind a disputed technical call>** — <passes>. Reconciled: <kept / changed to> <position>, because <reason>. If wrong: <consequence>.

   **Checked, no longer assumptions:**

   - ✅ <fact> (<source>).

   **Owed to other issues — not yet done there:**

   - #<m>: <what it needs, one line>.

   Passes: [<engine>](url) · … · [Reconcile](url) · [Correction](url)

   <!-- hgrill-assumptions:end -->
   ```

   `gh issue edit <n> --body-file <scratch file>`.

5. Add the hold label, whatever state the chain ended in, closed issues included: `gh issue edit <n> --add-label "<hold label>"`. Create it first if missing (`gh label create "<hold label>" --description "Automated grill done; a human must check the assumptions block"`). Keep `dev: agent` if the last handback set it, so sign-off is removing one label.
6. If `grill.verify_first_label` is set and the issue carries it, remove it: the passes are the verification it asked for. This is the one label removal the chain makes.

### Correction pass

A refuted premise means a decision was made on a false fact. Fix it here; never hand it to the owner as a finding.

1. Run one more pass on the chain's first engine, same kind and prompt (`pass <total+1> of <total+1>`), plus:

   ```text
   This is a correction pass. A read-only check after the last pass refuted these premises:
   - <false belief> — fact: <fact> (<source>). Affects: <decision, with its comment URL>.
   Re-decide every decision that rests on them, and anything that follows from those. Keep every other decision as it stands; do not reopen agreed calls. Your decision comment supersedes only the decisions you change, and names them.
   ```

   Answer it as any other pass.

2. Re-run Assumptions from step 1. A corrected premise goes to the checked list as `✅ <fact> (<source>; corrected in pass <k>)`.
3. If a new check refutes something again, run a second correction pass the same way on the chain's second engine. After that, any premise still refuted is a block: post the block with the `Refuted and not resolved` section, add the hold label, and follow [Blocks](#blocks).

### Done

Print this verbatim in the driver pane, then stop:

```text
════════════════════════════════════════════════════════════
 GRILL CHAIN DONE: <repo>#<n> <title> (<kind>)
 <engine>: <outcome> · <engine>: <outcome> · … · issue <open|closed>
 Reconcile: <k> technical re-decided (<c> changed), <p> product calls left to you.
 Correction: <none | pass <k> fixed <r> refuted premise(s) | blocked>.
 Product calls and assumptions posted to the top of the issue; held with <hold label>.
 THIS TAB IS INFORMATION ONLY. Do not type here; nothing is running.
════════════════════════════════════════════════════════════
```

Then rename the tab `G<n> ✓`, notify `grill chain done; assumptions need your check`, release the lock, and leave the tab open.

## Sign-off

When the user says they have checked an issue and accept it, any session may record it: in the block, replace the `> [!IMPORTANT]` callout with `> [!NOTE]` and `**Automated grill — checked and accepted by the issue owner on <yyyy-mm-dd>.**` followed by the same sentence about the passes; keep the lists; remove the hold label. If they reject some calls, leave the hold and the callout and start a correction run with `--owner`.

## Blocks

A block stops this chain only; other tabs keep going. Leave GitHub as it stands and the lock in place, rename the tab `G<n> ⚠`, and follow herdr.md, Blocks. Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. Resuming is the user's call: they name the pass to rerun, and the driver starts there with the earlier passes' comment URLs.
