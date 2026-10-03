# Everyday workflows

This page covers the skills most teams use day to day, and how they fit together:

```
product-grill / grill  →  backlog-refinement  →  issues start / agent-loop  →  critique / review chain  →  ship-staging
   decide what to build      make it agent-ready        implement it               review the draft PR       merge (optional)
```

Each section says what the skill is for, how to start it, and what it will and will not change. The installed `SKILL.md` is the full contract; read it when you need more than this page.

## Invoking a skill

| Harness     | How to start a skill                                                       |
| ----------- | -------------------------------------------------------------------------- |
| Claude Code | Type `/<skill> [args]`, for example `/grill 42` or `/issues ready --agent` |
| Codex       | Name the skill in the request: "use the grill skill on issue 42"           |
| Gemini/Agy  | Name the skill in the request, as with Codex                               |

In Claude Code, `grill`, `product-grill`, `backlog-refinement`, and `agent-loop` start only when you invoke them. Claude will not start them on its own because a conversation looks like an interview or a triage session.

## Install what these workflows need

For personal use, `add` is enough for `diagnosing-bugs`, and for `grill` and `product-grill` as free-form interviews:

```bash
npx activeloom add diagnosing-bugs grill product-grill --harness claude
```

Everything else here needs a repository installation. `backlog-refinement`, the `grill next` queue, `agent-loop`, and the review chain run scripts from the repository's harness root and read repository-level files such as `.backlog/refinement.local.md`. Install into the consumer repository with `npx activeloom init` (see [Getting started](getting-started.md#tier-1)). Include the Codex harness even if you mostly use Claude or Gemini: the automatic review chain's runner lives there.

All of them need an authenticated `gh` for anything that reads or writes GitHub issues and PRs.

## `issues` — the day-to-day issue workflow

Use `issues` to find work, claim it, and take one issue to a draft PR. Prefer `issues start <n>` over starting work by hand: it assigns the issue to you first, so a teammate or `agent-loop` does not pick up the same issue.

| Command                            | What it does                                                                                                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `issues` or `issues ready`         | Lists open issues with no open blockers, by priority. `--agent`, `--mine`, `--priority`, `--area` filter it |
| `issues show <n>`                  | Reads an issue and its dependency references. Changes nothing                                               |
| `issues <n>` or `issues start <n>` | Checks the issue's scope, claims it, creates a worktree, implements it, validates, and opens a draft PR     |
| `issues start <n> --setup-only`    | Stops after the scope check and worktree setup                                                              |
| `issues claim <n>`                 | Assigns the issue to you and comments that you have claimed it                                              |
| `issues link <a> blocks <b>`       | Writes matching `Blocks` / `Blocked by` references into both issues                                         |
| `issues close <n> [msg]`           | Closes an issue. The agent asks first when the issue is not yours                                           |
| `issues search <query>`            | Runs a GitHub issue search                                                                                  |

`ready` leaves out issues marked `status: blocked`, anything with an `agent-bail:` label, issues whose `Blocked by #N` or `Depends on #N` references are still open, and issues that an open or recently merged PR already closes.

`start` stops and reports instead of guessing when the issue is closed, assigned to someone else, blocked, or already has an open PR. It also stops when the issue fails its scope check: no stated outcome, no checkable definition of done, too big for one PR, or an open design choice. That last case is the cue for `grill`. `start` ends at a draft PR. Review happens afterwards in a fresh session.

## `diagnosing-bugs` — find the cause before fixing

Use `diagnosing-bugs` when something is broken, failing, flaky, or slow and the first read of the code didn't explain it. You can invoke it directly (`/diagnosing-bugs` in Claude Code), or just describe the bug: the agent loads it on its own when you ask it to debug or diagnose something. `issues start` also runs it first for a bug report that describes symptoms without a verified cause.

It works in phases:

1. **Build a feedback loop.** Before reading code for theories, the agent builds one fast, repeatable command that goes red on your exact symptom: a failing test, a script against a dev server, a replayed payload, or a bisection harness. This is most of the work. If it can't build one, it stops and asks for an environment, an artifact, or a decision rather than guessing.
2. **Reproduce and minimize** until every remaining piece of the repro matters.
3. **Hypothesize.** It shows you 3–5 ranked, falsifiable hypotheses before testing any, so you can re-rank them from what you know.
4. **Instrument** one variable at a time, with tagged debug logs that are easy to remove. For performance regressions it measures a baseline and bisects against it instead.
5. **Fix with a regression test**, written first and watched failing, where the code has a seam that can reach the bug. If there is no such seam, it records that as a finding.
6. **Clean up.** Debug logs and throwaway harnesses are removed, and the confirmed cause goes in the commit or PR.

It builds repros on synthetic data and keeps secrets and user content out of anything it shows, logs, or posts. It needs no repository installation: `npx activeloom add diagnosing-bugs` is enough.

## `agent-loop` — work the ready queue unattended

`agent-loop` implements issues without you in the loop. For each issue it claims the issue, creates a branch and worktree, has a worker implement it, opens a draft PR, and runs local review rounds: Codex first, then Claude. It works best after `backlog-refinement` has tagged a queue of `dev: agent` issues.

```text
/agent-loop --issues 12,15 --dry-run   # show what it would pick up
/agent-loop --issues 12,15             # implement exactly these issues
/agent-loop --issues 12,15 --isolate ../batch-12-15
/agent-loop 3                          # take up to 3 issues from the ready queue
```

`--issues` is a strict allowlist. Without it, the loop takes unassigned `dev: agent` issues from the `issues ready` queue. Keep early runs small and allowlisted until the queue has earned your trust. `--include-assigned` also takes issues assigned to you, and `--resume` continues a stopped run.

Use `--isolate <new-directory>` when other worktrees need to keep working concurrently. It creates an independent repository and a linked controller, preserving the existing per-issue worktree and review flow. Bootstrap files must be committed on the configured base. Keep the directory for recovery and resume using the printed controller path. Global Git settings and credentials remain shared; see the installed agent-loop skill for the isolation boundary.

**Before the first run:**

- Install and authenticate the `claude` and `codex` CLIs (both are required whichever harness you start from), plus `gh` and `jq`.
- Create a review profile with `review-setup`. Without one, the loop refuses to start.
- Fill in `agent-loop.config`, `prompt.txt`, and `agent-loop-instructions.md` in the skill directory. They are created from templates on first run. Set `base_branch` to your integration branch and the validation hook to your real pre-push gate. Put your repository's data and security rules in the instructions file, since it is all the worker knows about the project. `config-doctor.py` checks the result; [Set up for autonomous development](autonomous-development.md) walks through it.

Each issue ends one of three ways:

- **Ready PR.** The PR is marked ready only after a clean review round.
- **Draft PR.** Review hit its round cap without a clean round, so the PR stays a draft for a person to pick up.
- **Bail.** The worker decided it could not finish. The loop releases the claim, removes the worktree, and leaves a handoff note asking you to add an `agent-bail:` label. Run `backlog-refinement rca` afterwards to turn bails into rubric fixes.

The loop never merges, never calls hosted reviewers, and never copies logs or issue text into GitHub.

## `backlog-refinement` — make the backlog agent-ready

`backlog-refinement` checks every open issue against an agent-readiness rubric. Issues an unattended agent can finish are rewritten into a standard template and tagged `dev: agent`. Every other issue gets an `agent-bail:` reason and, where an interview would unblock it, a `needs: grill` or `needs: product-grill` label.

| Mode                 | Use it to                                                                                            |
| -------------------- | ---------------------------------------------------------------------------------------------------- |
| `setup`              | Create `.backlog/refinement.local.md`, `.backlog/learnings.local.md`, and the labels. Run this first |
| `queue` (default)    | Count what is ready, excluded, waiting on an interview, and not yet refined. Changes nothing         |
| `assess <n>`         | Dry-run one issue: the verdict, labels, and rewritten body it would produce                          |
| `refine --limit 5`   | Refine a small sample. Review every result before going bigger                                       |
| `refine --batch [N]` | Refine the next N issues (default 25) with parallel read-only assessors and one reviewed apply       |
| `refine --backfill`  | Add missing priority and `needs:` labels to already-assessed issues                                  |
| `rca [since]`        | After an `agent-loop` run, turn each bail into a rubric or instruction fix                           |

Refinement edits issue bodies (keeping the original in a blockquote), labels, and comments. Set the rewrite mode to `suggest` in the local file if you want rewrites posted as comments instead. Refinement does not close or reassign issues. The one exception is a verified-stale issue when the local file sets `stale-action: close`.

Start with `queue`, clear its **re-verify** bucket (issues tagged `dev: agent` by something other than refinement), sample with `refine --limit 5`, then work through the rest in batches. In an early, product-heavy repository, most issues end up excluded. That is expected. The excluded issues feed the interview queues below. [Set up for autonomous development](autonomous-development.md) walks through a first refinement in detail.

## `grill` and `product-grill` — settle the open questions

Both skills interview you. Neither writes code.

- **`product-grill`** decides what to build and whether to build it: who it is for, what problem it solves, and what evidence exists. It can end with "don't build this" or "gather evidence first." It is for anyone doing product discovery, not only product managers. It asks your role and how much code you read, saves those answers to `~/.config/activeloom/product-grill.json`, and words its questions to match.
- **`grill`** decides how to build something: it maps the design as a tree of decisions and works through it until nothing is silently assumed.

Run `product-grill` before `grill` when the product intent is still open.

Each skill works in rounds. Every question comes with a recommended answer, so you can agree, adjust, or push back. The agent looks up facts in the code, git history, and issues itself. You make the decisions. The session ends when no open decisions remain, with a summary of the decisions for you to confirm.

**Starting a session:**

| Invocation           | Interviews                                                           |
| -------------------- | -------------------------------------------------------------------- |
| `grill <idea>`       | A free-form idea, plan, or design                                    |
| `grill 42 43`        | The named issues, starting from the question refinement left on them |
| `grill next`         | The highest-priority issue labeled `needs: grill`                    |
| `product-grill next` | The highest-priority issue labeled `needs: product-grill`            |

**Handing back to refinement.** After you confirm the summary on an issue-driven session, the interview drafts a decision comment. It then asks refinement to dry-run the issue against that decision and shows you one combined preview: the comment, the new verdict, label changes, any rewritten body, any child issues for a split, and any proposed close. After you confirm, the interview posts the decision comment and refinement applies the rest. Each close needs its own explicit yes. A settled decision does not make an issue agent-ready by itself; refinement still judges it against the rubric.

To work through the backlog's interview queue, repeat `product-grill next` and `grill next` until `backlog-refinement queue` shows both queues empty or the remaining issues need someone else.

## `critique` and `deepcritique` — review a PR in one session

Run these in a fresh session, not the one that wrote the change. Both start from a draft PR (opening one if needed). They post each verified finding inline before fixing it, then push the fix, reply, and resolve the thread.

- **`critique [PR]`** runs a Lean review: the highest-signal review lenses, for up to two rounds.
- **`deepcritique [PR]`** runs a Deep review: a one-time cleanup pass (`refactorpass`), then `critique` with the full set of relevant lenses, for up to four rounds. Rounds 3 and later fix only blocking defects.

You don't have to pick the tier by gut feel. The review workflow chooses Deep when a change touches security or data boundaries, can't be reverted, fans out to other repositories, or changes subtle runtime behavior. `deepcritique` hands a change that resolves to Lean back to `critique`. `critique deep` or asking for a deep review forces Deep.

Both need a review profile (`review-setup`), a clean committed feature branch, and the Codex harness installed in the repository, since the run controller lives there. Repository-specific review lessons go in `.review/addendum.local.md`, which both read. A change that only needs a human glance, such as docs-only, ends immediately with no review. A change of fewer than 20 lines of code or configuration ends with a recommendation to read and merge it; ask for the chain to run it anyway.

Each run ends `clean`, `changed`, or `blocked`, and reports the tier, rounds, and fix commits. Neither skill merges, force-pushes, or marks a PR ready while threads are still open.

## The automatic review chain

Where `critique` is one engine reviewing in your session, the automatic review chain runs several independent reviewer engines (Claude, Codex, and Gemini) over one draft PR, one pass after another. A deterministic runner script controls it, not the conversation. Each pass reviews the current head, posts verified findings inline, pushes fixes, and resolves its threads. The runner verifies each pass, runs your validation gate, and posts an attestation before starting the next pass.

### Before the first run

1. **Install the Codex harness in the repository.** The runner lives at `.codex/skills/critique/scripts/review-chain-runner.py`, so include `codex` when you run `init`, even if you start reviews from Claude.
2. **Install and authenticate each reviewer's CLI**, plus `gh`.
3. **Create your review profile** with the `review-setup` skill, or `npx activeloom review-config init --accept-defaults` after checking `npx activeloom review-config defaults`. It records each reviewer's model and effort and the order the engines run in. The runner refuses to start without a confirmed profile.
4. **Declare the validation gate** in `.activeloom-review.json` at the repository root: the commands to run after every pass, chosen by the paths that changed. The [runner reference](../.codex/references/review-chain-runner.md#repository-declared-validation) has the schema. Check it with `python3 .codex/skills/critique/scripts/review-chain-runner.py --validate-contract`. Repositories without the file pass each gate command with `--check` instead.

### Start a run

Open a draft PR from a clean, dedicated worktree, then ask your agent:

```text
Run the automatic review chain on PR #42.
```

The agent resolves the review tier (Lean, or Deep when the change triggers it), reads the engine order from your profile, writes an authorization note that the runner posts to the PR, and starts the runner. Claude Code runs it in the background and reports when it finishes. Ask for a deep review explicitly if you want one. Asking for "the review chain" alone does not select Deep.

To start it yourself, run from the worktree root:

```bash
python3 .codex/skills/critique/scripts/review-chain-runner.py \
  --repo example/project --pr 42 --base <merge-base-sha> \
  --author claude --tier lean \
  --cycle codex,claude --until-converged \
  --authorization-file /absolute/path/review-authorization.txt
```

`--cycle` repeats two or three engines until they converge or the tier's cap is reached. The cap is two passes per engine at Lean and four at Deep. `--chain codex,claude,codex` runs an exact list of passes instead. Add `--resume` with the same arguments to continue a stopped run.

### Read the outcome

| Outcome       | Exit | Meaning                                                                             |
| ------------- | ---- | ----------------------------------------------------------------------------------- |
| converged     | 0    | Every required pass ran and the current head has clean, independent review evidence |
| plan-complete | 3    | A fixed `--chain` finished, but the evidence does not show convergence              |
| exhausted     | 3    | The cycle hit its cap without converging                                            |
| blocked       | 2    | Something needs a person: a failed gate, a changed head, a missing result           |

A blocked run prints the exact command to resume it. Keep the checkpoint directory under `.git/activeloom-review/` until the run is resolved. Deleting it does not grant a new budget.

The runner never marks a PR ready and never merges, even when the run converges. That decision stays with you and your repository's merge policy.

For the review protocol behind the chain, see your harness's `REVIEW_WORKFLOW.md` ([Claude](../.claude/REVIEW_WORKFLOW.md), [Codex](../.codex/REVIEW_WORKFLOW.md), [Gemini/Agy](../.agents/REVIEW_WORKFLOW.md)). For recovery, retries, and telemetry, see the [runner reference](../.codex/references/review-chain-runner.md).

## `ship-staging` — merge to a staging branch (optional)

`ship-staging` fits only repositories that integrate on a `staging` branch and promote to `main` separately. It ships with the Codex and Gemini harnesses, not Claude.

`ship-staging <PR>` checks that the PR targets `staging`, has every required check passing, and has no requested changes. It then asks you to confirm, marks the PR ready, and merges it with a merge commit. After merging it:

- labels the linked issues `status: on-staging` and removes `dev: agent`, so `issues ready` and `agent-loop` stop offering them;
- fast-forwards your local staging checkout; and
- posts a short "what shipped" message to a Google Chat webhook.

It refuses to merge unless `GCHAT_DEV_WEBHOOK_URL` or `GCHAT_DEV_WEBHOOK_FILE` is set and the `status: on-staging` label exists. It does not close issues; they close when `staging` is promoted. It never squashes, rebases, or bypasses branch protection.

## Where to go next

- [Set up for autonomous development](autonomous-development.md) connects these skills into an unattended pipeline, with `agent-loop` implementing the ready queue.
- [Agent entry guide](agent-guide.md) maps every other skill to the task it fits.
