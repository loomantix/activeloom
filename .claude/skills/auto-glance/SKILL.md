---
name: auto-glance
description: Automated glance — one non-author model reads a pull request that the review workflow sent to human glance (docs/config only, or a small change), posts a structured result at the exact head, and marks the PR ready when the read is clean and CI is green. Opt-in per repository.
disable-model-invocation: true
argument-hint: '<pr> [--author claude|codex|gemini]'
---

# /auto-glance — one peer read in place of a human glance

Usage: `/auto-glance <pr> [--author claude|codex|gemini]`

The review workflow's human-glance gate stops a PR that is docs/config only (`skip`) or under the small-change limit (`smallChange`) and asks a person to read the whole diff and merge. When the repository opts in, this skill puts one non-author model in that seat. It runs no review chain, writes no ledger run, roster, tier marker or attestation, and never edits the PR. Design: loomantix/activeloom#422.

This session is the **glancer**. It drives one reviewer pane; it never reads the diff as the reviewer itself. Mechanics: [`herdr.md`](../../references/herdr.md).

## Hard stops

- never merge, and never mark a PR ready except as [Decide](#steps) says;
- never push to the PR branch, and never let the reviewer push;
- never drop or downgrade a reviewer finding.

## Paths

Run from the repository's primary checkout.

- `L` = `.claude/skills/critique/scripts/review-ledger.js`
- `P` = `.claude/skills/review-setup/scripts/review-profile.py`
- `C` = `.claude/skills/hreview/scripts/herd-config.py`
- State and scratch: `python3 -I $C paths --repo <repo> --skill auto-glance --key pr-<n>`.

## Steps

1. **Preflight.** `test "${HERDR_ENV:-}" = 1`, or stop. `python3 -I $C repo`: if `glance.auto` is not `true`, say the repository has not opted in, hand back to a human glance, and stop. Read the PR: `gh pr view <n> --json state,isDraft,baseRefName,headRefName,headRefOid`. It must be open. Use full SHAs. `git fetch origin <base> <headRefName>`.
2. **Confirm it is a glance.** `node $L classify-changeset --base $(git merge-base origin/<base> <head>) --head <head>`. Continue only if `skip` or `smallChange` is true; otherwise the PR needs the chain: say so and hand back (`/hreview <n>`). If `.review/addendum.local.md` has a `## Review tier extensions` section and the diff touches a surface it lists as always-Deep, the PR is not a glance at any size: name the surface and hand back for a Deep chain. If the PR's comments already carry a `local-review-run:v1` marker, a chain owns it: stop.
3. **Pick the reviewer.** Author: the engine named by the PR commits' `Co-Authored-By` trailers, or `--author` when given (it wins over the trailers). Reviewer: the first engine in `python3 -I $P order --tier lean --repo <repo>` that is not the author. Model and effort: `python3 -I $P launch-args --engine <reviewer> --repo <repo>`. No identifiable author, or no other engine available: alert and stop.
4. **Start the reviewer** in a new pane below this one, in the PR's worktree (reuse the one on the PR branch; otherwise add one under the user's `worktree_root`), named `<n>-glance`. It posts a comment, so it is not a read-only session; the prompt sets its limits.
5. **Prompt it**, then drive it to settled as `/hreview` does:

   ```text
   You are the one reviewer standing in for a human glance on <repo> PR #<n>,
   base <base sha>, exact head <head sha>. Read the whole diff
   (git diff <base>...<head>) and every file it touches as needed. Do not edit,
   commit, push, or run another review skill.
   Report every issue you believe is real, each with severity
   (blocking/major/minor/nit), confidence (high/medium/low), file:line, and one
   or two sentences. Do not filter by severity or confidence.
   Then post exactly one PR comment with gh, under 400 words, that starts with
   <!-- auto-glance:v1 engine=<reviewer> model=<model> head=<head sha> -->
   followed by a one-line verdict and the findings list (or "No findings.").
   Reply with the comment URL.
   ```

6. **Decide.** Read the comment the reviewer posted; its marker must name the exact current head (re-read `headRefOid`; a moved head means start over from step 1).
   - **Clean:** no finding at `blocking` or `major` (minor and nit can stand, listed in the comment). Wait for every CI run at the head to complete (`gh run list --commit <head>`); all `success` or `skipped` and at least one run → `gh pr ready <n>`, then reply on the PR with `<!-- auto-glance-result:v1 head=<head sha> outcome=clean -->` and `Auto-glance clean at <head12> (<reviewer> <model>); ready to ship.`
   - **Not clean:** any `blocking` or `major`, red or absent CI, or a reviewer that could not finish after one relaunch. Post the result line with `outcome=findings` (or `outcome=blocked`), leave the PR as it is, and alert.
7. **End the reviewer session**, title its pane `done@<head12>`, and report under 150 words: outcome, reviewer engine and model, head, findings by severity, CI.

## Alerts

Notify as herdr.md describes, with `--sound request`. Also send a `PushNotification` (one line under 200 characters, leading with the action needed) when the user's herd config has `push_notifications` on. For findings, the user's options are: fix and re-glance, or ask for the full chain (`/hreview <n>`).
