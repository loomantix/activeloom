---
name: manual-tests
description: Build or update the manual test plan for reviewed PRs or the integration branch — cases with stable IDs in a portable plan file, rendered as an Artifact checklist whose shared database keeps every tester's results. Report-only; never runs automated suites or gates a merge.
disable-model-invocation: true
argument-hint: '<pr> [<pr> ...] | integration [<pr> ...] | status [<pr> ... | integration]'
---

# /manual-tests — a checklist a person runs by hand

Usage:

- `/manual-tests <pr>`: one PR, tested on the local stack at its head.
- `/manual-tests <pr> <pr> ...`: several PRs tested together on one local stack.
- `/manual-tests integration [<pr> ...]`: what is on the integration branch and not yet on production, tested in the deployed integration environment. Naming PRs limits the plan to those merged PRs.
- `/manual-tests status [<scope>]`: read the results back and print the summary. Builds and publishes nothing.

The plan is a portable file, `plan.json`, in the shape of [`scripts/plan.schema.json`](scripts/plan.schema.json). The Artifact page is one front-end over it: the page embeds the plan and saves each result to its own database. `P` = `.claude/skills/manual-tests/scripts/plan.py` in the repository, else `~/.claude/skills/manual-tests/scripts/plan.py`; it owns case identity, revisions, staleness and the summary, so the rules below hold however a run goes.

## Contract

- **Report-only.** Results inform the user. They never hold, block or approve a merge or promotion, because anyone with Contributor access can write them.
- **Manual cases only.** Automated suites are never cases and this skill never runs them. A change covered only by automated tests gets `manual: false` with a one-line reason.
- **Every change in scope** gets at least one case or a "no manual test needed" reason, commits not mapped to a PR included.
- **Authorization.** Invoking this skill, or accepting a horch or hpromote offer, authorizes publishing to a private Artifact: the repository, PR numbers and titles, commit SHAs, test steps you write, and non-secret environment URLs and install steps the repository documents. Nothing else leaves the machine: never raw PR bodies, review comments, logs, secrets, or personal or customer data. Write every step yourself, with synthetic example data.
- **Notes stay here.** Tester notes on failed cases may be read in this session only. Everything else (`state.md`, `plan.json`, other panes, notifications, GitHub, logs) gets case IDs, statuses, counts and SHAs. `P apply-results` drops notes and references by construction.
- **Never posted.** The Artifact URL goes to the user and `state.md`, never to GitHub.

## 1. Scope, paths and lock

1. `repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)`. Read the repository's `AGENTS.md` or `CLAUDE.md` if not already in context.
2. Branches. `git fetch origin`, then read `.activeloom-herd.json` from the remote default branch with `git show origin/<default>:.activeloom-herd.json`; when it names an `integration_branch`, read the copy on that branch instead. Integration = its `integration_branch`, else the default branch. Production = its `promotion.production_branch`; the integration scope needs one, so without it stop and say so. UX globs = `manual_tests.ux_globs`, else none. A missing file means defaults; an unreadable one is a stop.
3. Paths: `python3 -I $P paths --repo $repo --kind pr|prs|integration [--pr <n> ...]`. It prints the scope `key`, the state `dir`, the `plan` and `state` paths and the page `title`. For `integration` pass no `--pr`; a named subset is applied in step 4.
4. `python3 -I $P lock --dir <dir>`. Exit 3 means another run holds this scope: show its line and ask the user. Release it with `unlock` at the end of the run, whatever the outcome.

**Done when** the lock is held and you know the repository, branches, globs and scope key.

## 2. Find the page and its results

1. Read `state.md` in the scope directory. It holds identifier lines only: `artifact: <url>`, `built: <head per change>`, and the `offered:` lines horch writes. Keep every line you do not own.
2. No `artifact:` line: list Artifacts (`Artifact` `action: "list"`, enough `limit` to cover the gallery) and match the exact title from step 1. One match: adopt it and record it. None: this run creates the page. More than one: stop and ask which to keep.
3. With a page: load `ArtifactData` (ToolSearch) and `list` the `results` collection with `out_dir` set to a new directory in your scratchpad, following `next_cursor` until the last page. Run `python3 -I $P apply-results --plan <plan> --results <that directory>`, then delete the directory: it holds tester notes. A failed read, a page you cannot open, or an unreadable `plan.json` (exit 3) is a stop: say what failed and change nothing.
4. For `status`, run `python3 -I $P summary --plan <plan>`, give it with the URL, unlock and stop.

**Done when** every existing result is folded into `plan.json`, or you know the page is new.

## 3. Gather the changes

- **PR scopes:** for each PR, `gh pr view <n> --json number,title,body,headRefOid,state,isDraft,files,closingIssuesReferences`. Convergence: list the PR's comments and find the latest `<!-- local-review-run-end:v1 … outcome=converged head=<sha> -->` from the authenticated GitHub actor. When none matches the current head, ask the user once whether to build anyway. On yes, record `converged: false` in `review`; the page then says "not converged at `<sha7>`".
- **Integration:** `git log --first-parent --format='%H %s' origin/<production>..origin/<integration>`. Map each merge commit to its PR (`gh pr list --state merged --search <sha> --json number,title,headRefOid,mergeCommit`); a commit no PR owns is change `commit-<sha7>`. With named PRs, keep only those; one that is not in the range is an error to report. `heads.integration` and `heads.production` are the two branch heads.
- **Promoted since the last run** (integration only): an `in_scope` change in the existing plan that is out of the range and whose merge commit is now an ancestor of production (`git merge-base --is-ancestor`) goes in `promoted`, with today's date and the production head. One still in neither branch's range is removed; `P` retires its cases.

Read for each change what decides its cases: the body's `Manual checks`, test plan and "still owed" items, linked issues, findings the review ledger deferred that need a person to confirm, and the changed paths.

**Done when** every change in scope has its head, title, merged state and those inputs.

## 4. Choose the cases

- Items under `Manual checks` or "still owed", and deferred findings that need a person, always become cases.
- Otherwise decide from the changed paths: a match on the UX globs means the change needs manual cases; with no globs, judge from what the paths do (a screen, a device, audio, an install, a deploy-time behaviour). A change with no manual surface gets `manual: false` and its reason.
- **Stable IDs.** Read `plan.json` first. A check that already has a case keeps its ID, even when you reword it; a new check gets a new ID: `pr<n>-<slug>` or `commit-<sha7>-<slug>`, slug lowercase with hyphens. Never give an existing ID to a different check.
- **Each case:** a title; numbered steps with invented example data; `pass`, what a person sees when it works; `on_failure`, where to look (the screen, log stream or dashboard by name); `platform`.
- **Known failure with a fix in flight:** keep the case and add `fix: {pr, merged, head}`, where head is the fix's merge commit once merged. An unmerged fix shows the case as waiting, not as a retest.
- **Environment.** Take the local stack or integration URL, and the mobile install path, from `AGENTS.md` or `CLAUDE.md`; for an Android sideload, from the steps in `.claude/skills/phone-install/SKILL.md` when it is installed. When the repository does not document one, write `{"status": "not documented"}`, say so in the report, and never guess a URL or command.

**Done when** every change has cases or a reason, and every case ID is either reused for the same check or new.

## 5. Merge, render and publish

1. Write the proposal to your scratchpad: `repo`, `scope` (`kind`, `prs`), `target` (`local` for PR scopes, `integration`), `environment`, `install` (or null), `heads`, `review`, `changes`, `cases`, `promoted` (or null). Run `python3 -I $P merge --plan <plan> --proposal <file>`. Exit 2 names the field to fix. Exit 3 means existing state conflicts (an ID that belongs to another change, a plan for another scope): stop and ask. Its report lists added, revised, retired, promoted and reactivated cases and the changes that joined or left the scope; give the membership changes to the user.
2. `python3 -I $P render --plan <plan> --out <scratchpad>/manual-tests-<key>.html`. Follow `/artifact`'s synthetic-data gate on that file: read it end to end and confirm every value came from the plan you wrote.
3. Publish with the Artifact tool. A new page: `file_path` the rendered file, `capabilities: {"db": {}}`, `icon: "checklist"`, and a one-sentence description. An existing page: pass its `url` from `state.md` and omit `capabilities` and `icon`, so the stored database and its rows carry forward. A republish reloads every open view; results already saved stay in the database.
4. After the first publish, `ArtifactData` `list` of `results` once, and tell the user in one line that the page is live and empty.
5. A publish that fails, or a page that refuses the `db` capability, ends the run with the plan kept: report it so the caller can retry or continue.

**Done when** the page at the recorded URL shows this plan.

## 6. Record and report

Write `state.md`: `artifact: <url>`, `built: <ref>=<sha7> …`, plus the lines you kept. Unlock. Report, under 150 words: the URL, `python3 -I $P summary --plan <plan>`, what joined or left the plan, and any environment that is not documented.

## Result documents

Each save on the page is a new document in the `results` collection with its own generated ID, so two saves never overwrite each other:

| Field         | Value                                                                  |
| ------------- | ---------------------------------------------------------------------- |
| `case_id`     | the case ID                                                            |
| `case_rev`    | the case revision the tester's page showed                             |
| `basis`       | the case's `basis` from that same plan: change head, plus `+fix:<sha>` |
| `tested_head` | PR scopes: the change head; integration: the integration head          |
| `status`      | `passed`, `failed`, `blocked` or `skipped`                             |
| `ref`, `note` | optional opaque reference and free text; never copied into the plan    |
| `recorded_at` | ISO 8601 time of the save                                              |

A result counts only for the revision and basis it records. A changed case, a moved PR head or a merged fix makes earlier results stale; a merge of an unrelated PR does not, so on the integration page an untouched case shows "carried over" with the build it passed at. Another front-end writes the same fields.
