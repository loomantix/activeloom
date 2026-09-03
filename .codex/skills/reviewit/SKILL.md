---
name: reviewit
description: Post-push AI review orchestrator for pull requests. Use when the user asks Codex to run AI review on a PR, address Gemini or Copilot findings, dedupe reviewer comments, push fixes, reply in PR threads, or complete the platform review chain. Both modes fire Gemini Flash + Copilot only — no in-skill local review during the loop. Accepts a PR number, optional `deep` (4-iter cap + early-exit on no-fix iters + final `deepcritique`) which asserts the resolved review tier rather than choosing it, and optional `--resume`.
---

# Reviewit

Run the post-push review cycle for an open PR.

## Modes

- **Lean**: default. Fire Gemini Flash and request Copilot review, but use staggered handling: fix Gemini first, then fold in Copilot when it finishes. Cap at 2 iterations.
- **Deep**: if the argument includes `deep`. Same two reviewers as lean — no in-loop local Codex review. Cap at 4 iterations with an **early-exit when an iteration produces no `fix` resolutions** (defer/dismiss-only iterations don't justify re-firing reviewers on an unchanged HEAD — they'd just re-post the same findings). After the loop exits for any reason — clean, early-exit, or iter cap — invoke the `deepcritique` skill so fresh subagents review the PR's current state in a separate session. This replaces the prior pattern of running `codex review` inline during the polling loop, which routinely caused the orchestrator to drop out early because the inline-review sub-skill's prompt was self-contained.
- **Resume**: if the argument includes `--resume`, do not fire reviewers again. Load or reconstruct the review state for the current PR head, poll existing reviewer output, then fix/reply (and, in deep mode, run the final `deepcritique` if it didn't yet).

## State

Persist reviewer state locally so the long Copilot wait does not block an active Codex turn:

```text
.codex/reviewit-state/<pr>.json
```

Create the directory if needed. The state file is local agent bookkeeping; do not commit it. Store at least:

- `pr`: PR number
- `headSha`: PR head SHA at reviewer trigger time
- `startedAt`: UTC timestamp captured before firing reviewers
- `mode`: `lean` or `deep`
- `iteration`: current iteration number
- `geminiRunId`: workflow run id when discoverable
- `copilotRequested`: boolean
- `phase`: current reviewer phase, such as `awaiting-gemini`, `handling-fast-findings`, `awaiting-copilot`, `final-deepcritique` (deep mode only), or `complete`
- `handledCommentIds`: inline/review/issue comment ids already fixed or replied to
- `lastPollAt`: UTC timestamp of the most recent poll
- `deepcritiqueRan` (deep mode only): boolean — set once the final `deepcritique` invocation has returned

On state load, normalize the legacy `phase=final-deepgrill` value to
`final-deepcritique` and the legacy `deepgrillRan` key to `deepcritiqueRan`,
then rewrite the normalized state before continuing. A legacy key present with
its current counterpart absent is the expected migration input: normalize it
silently. Fail closed only on a genuine conflict — both spellings present with
different values — and do not guess which final review ran.

If `.codex/reviewit-state/` is not gitignored, add it to a repo-appropriate ignore file before writing state.

## Process

1. Parse arguments: first token is PR number. Optional tokens:
   - `deep`
   - `--resume`
   - `--wait` to keep polling until reviewers complete or timeout
2. Verify the PR is open and the local branch matches the PR head:

   ```bash
   gh pr view <pr> --json number,title,headRefName,baseRefName,headRefOid,state,files,mergeable,id
   ```

3. Skip or ask before spending reviewer budget on docs/config-only PRs.
4. Use an adversarial stance for all local review in this skill: assume there are problems to find, try to disprove safety with code/tests/docs evidence, and report only actionable findings with file/line support.
5. Bias toward fixing every valid finding in this PR, including nits and cleanup items. Dismiss only invalid findings, false positives, or suggestions that would make the code worse. Defer only valid but extremely large follow-up refactors, roughly 300+ lines or cross-cutting rewrites, and create/link a GitHub issue for each deferral.
6. If `--resume` is present:
   - Load `.codex/reviewit-state/<pr>.json` if present.
   - Normalize the legacy final-deepgrill phase/key as described above before
     branching on state, and persist the normalized form.
   - If no state file exists, reconstruct enough state from `gh pr view`, PR reviews, PR review comments, issue comments, and workflow runs. Use the current PR head SHA as `headSha`.
   - If the current PR head SHA differs from the saved `headSha`, ask whether to start a new iteration. Do not silently process stale reviewer output.
   - Do not trigger Gemini or request Copilot again unless the saved reviewer request clearly failed or the user explicitly asks to rerun.
   - Continue at the polling/dedupe/fix/reply step. If the saved `phase` is `final-deepcritique` and `deepcritiqueRan` is false, resume by invoking the `deepcritique` skill directly (skip the bot polling loop).
7. For each iteration up to the cap:
   - Capture current PR head SHA and timestamp before firing reviewers.
   - Write the initial state file.
   - Trigger Gemini Flash:

     ```bash
     gh workflow run "Gemini Code Review" --repo <owner>/<repo> -F pr_number=<pr> -F tier=flash
     ```

   - Record the Gemini workflow run id when discoverable:

     ```bash
     gh run list --repo <owner>/<repo> --workflow "Gemini Code Review" --limit 10 --json databaseId,status,createdAt,headBranch
     ```

   - Request Copilot via GraphQL bot reviewer:

     ```bash
     PR_NODE=$(gh pr view <pr> --json id --jq '.id')
     gh api graphql \
       -f query='mutation($prId:ID!,$botIds:[ID!]){requestReviews(input:{pullRequestId:$prId,botIds:$botIds,union:true}){pullRequest{id}}}' \
       -f prId="$PR_NODE" \
       -f botIds='BOT_kgDOCnlnWA'
     ```

   - Mark `copilotRequested: true` in the state file.
   - Set `phase: awaiting-gemini`. Poll PR comments, PR review comments, PR reviews, and the Gemini workflow run for Gemini output on `headSha`. Use a short active budget by default (60-90 seconds) unless `--wait` is present. Do not wait for Copilot in this phase.
   - If Gemini has not posted after the short budget, stop cleanly and report:
     - PR number
     - `headSha`
     - reviewers fired
     - reviewer status
     - state file path
     - exact resume command, for example `reviewit <pr> deep --resume`
   - Set `phase: handling-fast-findings`. Deduplicate Gemini findings by file, line, and root cause. Fix every valid finding, including nits. Defer only valid but extremely large follow-up refactors to GitHub issues. Dismiss invalid findings and false positives with rationale.
   - Commit and push the Gemini fixes before waiting on Copilot. If Gemini produced no `fix` resolutions (everything deferred or dismissed, or nothing actionable), skip the commit/push but still process replies.
   - Reply to every handled Gemini inline AI comment after pushing, including the fix commit SHA or rationale. Append handled comment ids to the state file so repeated resumes do not duplicate replies.
   - Set `phase: awaiting-copilot`. Now poll Copilot output for the original `headSha` and comments newer than `startedAt`. Copilot is allowed to be reviewing the pre-fix head; treat its findings as delayed feedback on that iteration.
   - If Copilot has not posted yet, poll briefly by default. If `--wait` is present, keep polling until Copilot completes or times out. If Copilot is still missing after the allowed wait, stop cleanly with the same state/resume details; do not start the next iteration until the pending Copilot request is handled or explicitly abandoned.
   - Deduplicate Copilot findings against already-handled Gemini findings and the current working tree. If a Copilot finding was already fixed by the Gemini commit, reply with that commit SHA and record it handled. Fix any remaining valid Copilot findings on top of the current head, push, and reply.
   - On resume, continue from `phase`: poll only reviewer output for the saved `headSha` and comments newer than `startedAt`, then handle only unhandled comment ids.
   - Loop control:
     - **Lean**: continue to the next iteration if any reviewer found new findings on the post-fix HEAD and the cap is not reached. Otherwise exit the loop.
     - **Deep**: continue to the next iteration only if this iteration produced ≥1 `fix` resolution (a commit was pushed) and the cap is not reached. If the iteration produced only defer/dismiss findings (or none at all), **early-exit** the loop — re-firing reviewers on an unchanged HEAD just re-posts the same findings.

8. **Deep mode only — final `deepcritique`.** After the loop exits for any reason (clean, early-exit, or iter cap), set `phase: final-deepcritique` and invoke `deepcritique <pr-number>`. It loads the existing PR ledger, runs `critique deep`'s six core lanes (code reviewer, silent failure hunter, type/API design analyzer, comment/docs analyzer, PR test analyzer, security reviewer) and the conditional tenant-coupling lane when signaled, posts verified findings inline before fixes, then replies and resolves. It runs `refactorpass` first only when this engine has not already spent its once-per-PR cleanup latch — on a PR that ran the pre-push chain it normally has, so expect a critique-only tail pass. If the PR is already three or more Codex rounds deep, `deepcritique` selects its convergence stance: narrowed lanes, blocking-defects-only fixes, and issues for the rest. Both decisions come from the PR ledger; do not override them from here. When control returns from the sub-skill, set `deepcritiqueRan: true`, capture the sub-skill's output, and continue to the summary step. **Do not stop after `deepcritique` returns** — `reviewit` owns the final summary.

## Important Details

- Pass `tier=flash` explicitly unless the user asks for Pro and accepts cost.
- Copilot may post inline comments without a review row; poll both endpoints.
- Do not count stale comments from a previous commit as completion.
- Do not retrigger Gemini or Copilot on `--resume` unless the PR head changed and the user starts a new iteration, or the original request failed.
- Prefer exiting with a resume command over waiting 5-10 minutes for Copilot during an interactive Codex turn.
- Do not reply before pushing fixes; replies should reference the real commit SHA.
- If a reviewer times out in `--wait` mode, continue with findings received and state the timeout in the summary.
- Keep `.codex/reviewit-state/*.json` out of commits.
- The final `deepcritique` in deep mode is **always** run after the loop exits — clean, early-exit, or iter cap. Fresh subagents are most useful precisely when the bot reviewer loop didn't fully converge, so do not gate `deepcritique` on the loop's exit reason.
- If `deepcritique` fails or is interrupted, record the failure in the summary and continue — the bot reviewer loop's output is still valid. Do not auto-retry.

## Output

Summarize:

- mode, iterations completed, reviewers fired and their status
- findings fixed / deferred / dismissed counts and links
- commits pushed
- replies posted (and any failed reply targets)
- deep-mode `deepcritique` result: the round it resolved and whether it ran adversarially or in convergence mode, whether `refactorpass` ran at all and if so whether it produced a commit, and the count of `critique deep` findings the user chose to fix / defer / ignore
- state file path if still waiting
- resume command if applicable
- any remaining risks
