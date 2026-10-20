---
name: promote
description: Promotion from a repository recipe — runs the integration-to-production steps the repository declares in `.activeloom-promote.json` (preflight commands, task briefs, a read-only risk review, the promotion PR, CI and deploy-workflow watches, post-merge checks), opens or adopts the promotion PR, and stops at the human approval and merge.
disable-model-invocation: true
argument-hint: '[--dry-run] [--integration-branch <branch>] | merged | validate [<file>]'
---

# /promote — recipe-driven promotion

Usage:

- `/promote [--dry-run] [--integration-branch <branch>]`: preflight, open or adopt the promotion PR, run the pre-merge steps, stop at the approval.
- `/promote merged`: the human merged the promotion PR; run the post-merge steps.
- `/promote validate [<file>]`: check a working-tree recipe while writing it.

This skill runs the recipe in one session, one lane after another. `hpromote` runs the same recipe across herdr panes and uses this skill's step rules.

## Recipe

`R` = `.claude/skills/promote/scripts/promote-recipe.py`. Its JSON schema, `promote-recipe.schema.json`, sits beside it.

`python3 -I $R show > <scratch>/recipe.json` discovers `.activeloom-promote.json` through the remote default branch, then reads the integration branch it names. With `--integration-branch <branch>`, pass `--default-branch <branch>` to `show` instead, and require the returned recipe's `integration_branch` to match. This supports a first recipe committed only on the integration branch while production remains the remote default. Both paths read from `origin`, never a working tree or PR head. Exit 4 means the starting branch has no recipe: ask for the integration branch if none was supplied; otherwise point at the schema and stop. Exit 1 or 2 is a block. `validate` checks a local file and exits the same way.

A recipe names `integration_branch` and `production_branch`, optional `guide` (promotion notes: read it before the first step) and `rollback` (runbook), `blocker_issues`, `pull_request` settings, and `steps`. Each step has an `id`, a `kind`, a `phase` and a `lane`.

Phases run in this order, with two fixed milestones the skill owns:

1. `preflight`, on the pinned integration head.
2. **Open or adopt the promotion PR.**
3. `pre_merge`, on the promotion PR head.
4. **Human approval and merge.** Never this skill's.
5. `post_merge`, on the merge commit.

Inside a phase, a lane's steps run in recipe order, and a failed step ends its lane. Different lanes of a phase are independent.

| Kind             | What runs                                                                                      | A failure is                                                                                                                                |
| ---------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `command`        | `run` in the inspection worktree, within `timeout_minutes`                                     | `on_fail`: `block` a blocker (fixable when `fix` is set), `hold` a stop for the user, `warn` a line in the PR body                          |
| `task`           | A session follows the `brief` file, read at the pinned head                                    | Whatever the brief says. `writes: false` is read-only; `writes: true` may act as the brief says, within the hard stops                      |
| `risk_review`    | A read-only review of the whole promotion diff ([Risk review](#risk-review))                   | A confirmed blocker or major finding is a blocker                                                                                           |
| `watch_checks`   | The promotion PR's `required` (or `all`) checks at its head                                    | A flake is rerun up to `rerun_flakes` times; a defect is a blocker                                                                          |
| `watch_workflow` | The newest run of `workflow` on the phase's commit: integration head, PR head, or merge commit | A blocker before the merge, a production incident after it. No run within five minutes is `absent`, or `skipped` when `if_absent` is `skip` |

A shell lane holds `command`, `watch_checks` and `watch_workflow` steps. Run it with:

```bash
python3 -I $R run --recipe <scratch>/recipe.json --phase <phase> --lane <lane> \
  --cwd <worktree> --sha <full sha> [--pr <n>] --out <scratch>/<phase>-<lane>.json
```

It streams each step's output, writes the result JSON (each step's `outcome`, and the `fix` argv of a failed command that has one), and prints `PROMOTE-LANE <phase>/<lane> <outcome>` last. Exit 0: the lane passed. Exit 3: a step did not; read the JSON. Exit 1 or 2: a block. A session lane holds one `task` or the `risk_review`; this session does it.

## Hard stops

- Never approve or merge the promotion PR, enable auto-merge on it, dispatch a deployment or run a production migration.
- Never bypass branch protection or use admin privileges.
- Never push to, reset, recreate or delete the integration or production branch. Fixes reach the integration branch through a PR.
- Never skip hooks or signing (`--no-verify`, `--no-gpg-sign`).
- Before every GitHub write, refetch the object and confirm repository, number, state, head, base and head SHA still match. Stop on a mismatch.
- Public PR, issue and comment text carries no secrets, tokens or raw logs; link the run instead.

## Standing authorization

Invoking this skill without `--dry-run` approves only:

- opening the promotion PR, or adopting an open one whose head is the integration branch, and adding `pull_request.labels`;
- closing an open promotion PR into the production branch whose head is not the integration branch, when `pull_request.close_stale` is true;
- for a failed `command` with `fix`: running `fix` in a new worktree cut from `origin/<integration>` and opening a PR with the change to the integration branch;
- the actions a `writes: true` task's brief names;
- rerunning failed jobs on the promotion PR, up to `rerun_flakes`;
- filing one issue per blocker no fix clears, with `blocker_issues.labels`, when `blocker_issues` is set. Search open issues first and comment on a match instead.

A dry run reads remote state and runs the recipe's commands in the inspection worktree. It makes no GitHub write, opens no fix, and runs a `writes: true` task only up to its first write, reporting what it would do.

## Run

### 1. State

1. `git fetch origin <integration> <production>`. Pin `I` = `origin/<integration>` and `P` = `origin/<production>`, full SHAs. If `git log --oneline P..I` is empty there is nothing to promote: report it and stop.
2. `gh pr list --base <production> --state open --json number,url,headRefName,headRefOid`. An open PR whose head is the integration branch is adopted in step 3 (a dry run reads it only). With `close_stale`, close another PR only after positively identifying it as a stale promotion using the repository guide's documented promotion marker or branch convention. A production base alone is insufficient; leave unrelated or ambiguous PRs open. A dry run reports candidates without closing them.
3. Create the inspection worktree at `I`: `git worktree add --detach <root>/<name>-promote-<yyyymmdd> I`, where `<root>` is the repository's worktree location from its agent guide, else a sibling of the primary checkout. Never switch the primary checkout.
4. Read `guide`, if set, from the worktree.

### 2. Preflight

Run every `preflight` lane at `I` in the inspection worktree. Then:

- A `block` failure with a `fix` argv: open the fix PR (above). The promotion waits until it is merged into the integration branch; report it as pending and stop. When it has landed, start again at step 1 on the new head. Never validate one head and promote another.
- A `block` failure without one, `absent`, `timeout`, or a failed task: a blocker. Report the evidence (step, command, exit code, run URL), file the issue if configured, and stop.
- A `hold`: stop and tell the user which step holds and why.
- A `warn`: carry it into the PR body.

Deploy surface: for each `pull_request.surface` entry, list the files of `git diff --name-only P..I` that match its globs.

Done when every preflight lane passed at `I`.

### 3. Promotion PR

With `--dry-run`, render the title (`python3 -I $R title --recipe <scratch>/recipe.json`) and the body below into the report, and go to step 4 with `I` as the head.

Otherwise refetch. If `origin/<integration>` is no longer `I`, return to step 1. Adopt the open PR from step 1, or create it: `gh pr create --base <production> --head <integration> --title "<title>" --body-file <file>`. The body holds:

- a categorized summary of `git log P..I` (features, fixes, chores, CI, dependencies, other);
- the deploy surface, and warned steps;
- the preflight lanes and their outcomes at `I`;
- `Merge method: <pull_request.merge_method>`, when set;
- an **After merge** checklist: each `post_merge` step (what it runs or watches) and each `pull_request.after_merge` item;
- the `rollback` runbook link, when set.

Refetch the PR and confirm its head branch, base and `headRefOid` = `I`.

### 4. Pre-merge

Run every `pre_merge` lane against the promotion PR head, passing `--pr <n>`. A dry run has no PR: it runs only the session lanes, against `I`, and lists the shell lanes it skipped.

- A failed check: read the failed job. A transient failure is rerun (`gh run rerun <id> --failed`) up to `rerun_flakes` times, then the lane runs again. A defect is a blocker: fix it on the integration branch through a PR, never on the production branch or in the promotion PR.
- Whenever the PR head moves, return to step 1, refresh the inspection worktree and run every `preflight` lane at the new head before running `pre_merge` again. The risk review may cover only the delta since the last head it reviewed.

Ready means: every `preflight` and `pre_merge` lane passed at the same current head, no confirmed blocker or major finding, and `gh pr view <n> --json mergeable` reads `MERGEABLE`. Adopting an existing PR requires the same current-head evidence.

### Risk review

Each PR in the promotion was reviewed on its own. This pass looks for what breaks when the whole set reaches production together. It is read-only and counts toward no review ledger. Read `git diff P..<head>` and look for:

1. Migrations: safety while old instances still run, lock and statement timeouts, ordering, declared bounds.
2. Production config: environment and secrets new code needs at boot; flags that default on unintentionally.
3. Cross-PR interactions: changes from different PRs that touch the same surface or user moment.
4. Telemetry and privacy: user-entered text or personal data reaching logs, metric labels or error trackers.
5. Infrastructure and alerting: anything that silently breaks alerting or fires on an idle production.
6. Anything existing production users would see as broken or surprising on deploy.
7. Each `focus` item in the step.

Record every finding with severity (blocker, major, minor), confidence 0-100, file:line and a concrete failure scenario. Classify each blocker and major as **confirmed** (the line does what the finding says and the scenario holds), **refuted** (say why) or **product call** (it turns on what users should get; the user decides). Minors are listed and do not block. Verdict: SAFE TO MERGE, MERGE AFTER FIXES, or DO NOT MERGE.

### 5. Hand-off

Stop at the approval. Report, under 300 words:

- mode, recipe source commit, `I` and `P`;
- the PR URL, commit count and check state (in a dry run, the proposed title);
- every lane and its outcome, the risk verdict and each finding's disposition;
- fix PRs, closed PRs, issues filed, and anything pending;
- the human actions: approve and merge (with the merge method), then run `/promote merged`.

### 6. Post-merge

On `merged`:

1. `gh pr view <n> --json state,mergeCommit` must read `MERGED`. Pin `M` = the merge commit.
2. Create a worktree detached at `M` and run every `post_merge` lane against `M`.
3. Work through each `pull_request.after_merge` item, using the read-only access the repository's agent guide documents. A green job is not proof the change is live; check deployed state where the guide shows how.
4. Remove the worktrees. Report each lane's outcome and each after-merge item.

A failed post-merge step is a production problem: report the evidence and the `rollback` runbook and stop. Never roll back on your own.
