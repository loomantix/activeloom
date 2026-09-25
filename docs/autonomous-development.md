# Set up a repository for autonomous development

This is the path an agent follows when someone asks it to "set me up to use ActiveLoom for my development." It takes a repository from nothing installed to a first `agent-loop` run over a refined backlog, then closes the learning loop. Any harness and any model can follow it: every step names a skill or a command and a signal that proves it is done.

```
install + sync → onboard → review-setup → backlog-refinement setup → agent-loop config
      → backlog-refinement queue / refine → agent-loop (allowlisted) → backlog-refinement rca
                     ▲                                                          │
                     └───────────── lessons sharpen the local rubric ◀──────────┘
```

`<root>` below means the selected harness's distribution root: `.claude/`, `.codex/`, or `.agents/`.

## Brief the user before touching anything

Start by telling the user, in plain terms:

- **What the pipeline does.** `backlog-refinement` sorts the issue backlog into work an unattended agent can finish and work that needs a person, and says which interview (`grill` or `product-grill`) each excluded issue needs next. `agent-loop` then implements an explicit list of ready issues, one worktree and one draft PR per issue, each reviewed locally by more than one model.
- **What it needs from them.** An authenticated `gh`; the Claude **and** Codex CLIs for `agent-loop`, with their own model access; the repository's toolchain on this machine. Say which of these are missing now.
- **What it will change, and when it asks first.** Repository files arrive through a reviewed PR. `backlog-refinement` creates labels, and during `refine` it edits issue bodies, labels, and comments. `agent-loop` claims issues, pushes branches, and opens draft PRs. Get a yes before each of those three.
- **What it never does.** Merge, deploy, close issues, mark a PR ready on its own authority, or trigger hosted reviewers. Closing and merging stay with people.

Then establish the current state (next section) and resume from the first incomplete stage. Report the stage you found before doing anything else, so the user knows whether this is a fresh install or a repair.

## Copy into a session

```text
Help me set up ActiveLoom for autonomous development in this repository.
Read https://github.com/loomantix/activeloom/blob/main/docs/autonomous-development.md
and follow it. Read this repository's own agent instructions first. Brief me
on what the pipeline does, what it needs, and what it will change, then find
the first incomplete stage and continue from there. Ask me the decisions the
guide says not to guess; recommend an answer for each, with the evidence.
```

## The stages

Each stage is done when its signal holds. When the signal already holds, say so and move on; do not redo the stage.

| #   | Stage              | Run                                                                                                 | Done when                                                                                                                             |
| --- | ------------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Install and sync   | CLI `init --sync --harness <id>` ([getting started](getting-started.md#tier-2))                     | `.activeloom-config.yml` exists and the sync workflow is on the default branch                                                        |
| 2   | Project facts      | `onboard` skill                                                                                     | No `TODO(activeloom):` marker remains, and `onboard` step 3 has confirmed both CLIs for `<root>/skills/agent-loop/agent-loop.config`  |
| 3   | Review profile     | `review-setup` skill                                                                                | `python3 -I <root>/skills/review-setup/scripts/review-profile.py check` exits 0                                                       |
| 4   | Refinement rubric  | `backlog-refinement setup`                                                                          | `.backlog/refinement.local.md` and `.backlog/learnings.local.md` exist with no `TODO(backlog):` marker, and every rubric label exists |
| 5   | Loop configuration | Edit `agent-loop.config`, `prompt.txt`, and `agent-loop-instructions.md` from their templates       | `python3 <root>/skills/agent-loop/scripts/config-doctor.py --project-dir .` passes                                                    |
| 6   | First refinement   | `backlog-refinement queue`, then the re-verify bucket, then `refine --limit 5`, then `refine --all` | The re-verify bucket is empty, and the user has reviewed the first five rewrites before the sweep                                     |
| 7   | First loop run     | `agent-loop --issues N,N --dry-run`, then the same allowlist without `--dry-run`                    | One or two issues reached a reviewed draft PR, or bailed with an `agent-bail:` label and an RCA stub                                  |
| 8   | Close the loop     | `backlog-refinement rca`                                                                            | Every bail from the run has a dated entry in `.backlog/learnings.local.md` naming the edit it produced                                |

Stage 5 is where most setups go wrong. `base_branch`, `setup_hook`, and `validation_hook` ship empty. `validation_hook` must be the repository's real pre-push gate, not a narrower test. The instructions file must carry the repository's data and security rules, because the worker reads it and nothing else about the project.

Keep the first loop run small and explicitly allowlisted. Omitting `--issues` falls back to the ready queue, which is fine only once the queue has been trusted through an RCA cycle.

## Decisions to settle with the user, not guess

Ask these in one round, each with a recommended answer and the evidence behind it. A plausible guess here steers what an unattended agent builds.

- **Integration branch.** The branch loop PRs target. It is not always the default branch: a `staging` → `main` promotion flow integrates on `staging`. When the integration branch _is_ the default, a PR's `Closes #N` closes the issue on merge; when it is not, issues stay open until promotion, and the rubric needs a rule for merged-but-unpromoted work.
- **Sensitive paths.** Where a non-trivial change needs a human reviewer even with green CI. Draft them from the repository's own review-tier triggers — whatever already selects its deeper review path usually belongs here — and name real paths, not categories.
- **Priority scheme.** Reuse what exists rather than creating a parallel one. Look at existing labels _and_ issue titles: a `[P0]`–`[P3]` title prefix is a priority someone already set, and the local rubric can map it to the label scheme.
- **Rewrite mode.** `edit` lets refinement rewrite issue bodies (the original is preserved in a blockquote); `suggest` posts the rewrite as a comment instead.
- **Harnesses and reviewer models.** Which harness trees to commit, and the reviewer and worker model and effort for `review-setup`. `agent-loop` needs both Claude and Codex regardless of which harness starts it.

## What real setups have taught

- **A tagged queue is not a trusted queue.** Issues tagged `dev: agent` by anything other than refinement have never been checked against the integration branch, yet they are exactly what the loop picks up. Stale issues — work already shipped — are the most common cause of a wasted iteration. Clear the re-verify bucket before the first loop run.
- **Per-harness copies drift.** Repositories that predate `.backlog/` kept a `RUBRIC.md` and `LEARNINGS.md` under each harness root, and the copies diverged in wording. `backlog-refinement setup` migrates them to the single repository-root layer; delete the old copies once migrated.
- **Excluded work needs a way back.** An exclusion without a next step just sits there. The `needs: grill` and `needs: product-grill` labels are the queues that turn excluded issues into ready ones: run the interview, then re-refine.
- **Repository-specific rules stay local.** Sensitive paths, label names, and incident history go in `.backlog/refinement.local.md`. A rule that would hold in any repository is an upstream candidate for `core-rubric.md`; record it under _Upstream candidates_ in the local file and propose it here.

## Report the result

Finish with the stage reached, each stage's signal as observed (not assumed), anything skipped or deferred and why, and the next action. When stopping short of stage 7, say what blocks it — a missing CLI, an unconfirmed decision, an empty ready queue — rather than presenting the setup as complete.
