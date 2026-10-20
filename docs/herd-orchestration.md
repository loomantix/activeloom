# Herd orchestration on herdr

The herd skills let one agent session drive other agent sessions in visible terminal panes. You watch every review pass, grill interview and build as it happens, and you can type into any pane to step in. The driving session carries the work to a defined stopping point without asking you, and it stops for you at the decisions that are yours: merging to production, product calls, and anything it cannot do safely.

They run on [herdr](https://herdr.dev), a terminal workspace manager for coding agents. Any of the three harnesses (Claude Code, Codex, Gemini/Agy) can drive, and any of them can be driven.

| Skill         | What it does                                                                                                         | Stops for you when                                         |
| ------------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `hreview`     | Runs the ordinary review chain on a PR, one engine per pane, until it converges or runs out of rounds.               | It converged (merge is yours), or it is blocked            |
| `auto-glance` | Puts one non-author model in the seat of the human glance for small or docs-only PRs. Opt-in per repository.         | The read found something, or CI is red                     |
| `hgrill`      | Grills an issue on each configured engine in turn, then posts the biggest product calls and assumptions for a check. | Always: the assumptions block waits for your sign-off      |
| `horch`       | Orchestrates a whole workspace: starts chains, builds, grills and promotions in tabs and ships converged work.       | A ship gate fails, a chain blocks, or a call is yours      |
| `hloop`       | Runs a batch of agent-ready issues: a build tab per issue, then a review chain, then a ship through `horch`'s gate.  | An issue's design is open, a gate fails, or a chain blocks |
| `hpromote`    | Runs an integration-to-production promotion with a read-only risk review and fix lanes.                              | The promotion PR is ready: approving and merging it        |

These are orchestration tools, not a second evidence system. Each review pass is the engine's ordinary interactive review, recorded in the PR ledger exactly as if you had run it yourself. Tiers, round caps, convergence and attestations are unchanged; for runner-owned, attested chains keep using the [automatic review chain](workflows.md#the-automatic-review-chain). The design records are loomantix/activeloom#419 (the live review chain) and loomantix/activeloom#422 (these skills).

## Before you start

You need:

- **herdr 0.9.0 or later**, and to run the driving session inside it. Every herd skill checks `HERDR_ENV=1` first and stops if it is not set.
- **ActiveLoom installed in the repository** with the harnesses you use, plus the **Codex harness** even if you never drive from Codex: the review run controller lives at `.codex/skills/critique/scripts/local-review-handoff.py`, the path the review workflow already names. See [Getting started](getting-started.md).
- **The review profile** set up (`review-setup`). The herd skills take every model, effort and engine order from it.
- **An authenticated `gh`**, plus `git`, `jq` and Python 3.
- For `horch` to merge anything: the `ship-staging` skill installed (today it ships in the Codex and Gemini/Agy roots; a Claude-driven `horch` reads the Codex copy) and a `ship` section in the repository's config. Without both, `horch` stops at "ready to ship" and merges nothing.

Check what is installed:

```bash
herdr --version
ls .claude/skills .codex/skills .agents/skills 2>/dev/null | grep -E '^(hreview|auto-glance|hgrill|horch|hloop|hpromote)$'
python3 -I .codex/skills/hreview/scripts/herd-config.py repo
```

## Three layers of configuration

| Layer          | Where                                                            | Holds                                                                                          |
| -------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Per repository | `.activeloom-herd.json`, committed, consumer-owned, never synced | Integration branch, worktree bootstrap, gate-fix rules, glance, ship, builds, grill, promotion |
| Per user       | The review profile, plus `~/.config/activeloom/herd.json`        | Models, efforts and order (profile); launch arguments, worktree root, push preference (herd)   |
| Per push       | The `horch` instruction file, local and never committed          | What this orchestration session covers, its sequencing and the decisions reserved for you      |

Repositories also keep their always-Deep review surfaces where they already are: `.review/addendum.local.md`, under `## Review tier extensions`.

### Per-user settings: `~/.config/activeloom/herd.json`

Optional. With no file, the defaults below apply. `ACTIVELOOM_HERD_USER_CONFIG` points at a different file.

```json
{
  "schema_version": 1,
  "worktree_root": "~/wt",
  "push_notifications": true,
  "engines": {
    "claude": {
      "launch_args": [],
      "permission_off_args": ["--dangerously-skip-permissions"]
    },
    "codex": {
      "launch_args": [],
      "permission_off_args": ["--dangerously-bypass-approvals-and-sandbox"]
    },
    "gemini": { "launch_args": [], "permission_off_args": [] }
  }
}
```

| Key                               | Default | Meaning                                                                                                                                                                                       |
| --------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `worktree_root`                   | `~/wt`  | Where the skills create worktrees. Keep it outside your primary checkouts.                                                                                                                    |
| `push_notifications`              | `true`  | Whether the driver also sends its harness's push notification (Claude Code has one) for events that need you. herdr's own notification is always shown.                                       |
| `engines.<e>.launch_args`         | `[]`    | Added to every launch of that engine.                                                                                                                                                         |
| `engines.<e>.permission_off_args` | `[]`    | Added to sessions that edit (review passes, builds, fixes); left out of read-only sessions such as the promotion risk review. Use the flag your CLI documents; the values above are examples. |

Permission-off flags belong here, not in a shell alias. The skills cannot see an alias, and a flag passed twice makes the Codex CLI exit, so an alias plus this file breaks every Codex launch. The helper rejects a flag that appears in both lists of one engine. Without permission-off flags, panes stop at every permission prompt and the driver answers them, which works but is slow.

Models and efforts do not go here. They stay in the review profile, which `review-setup` manages.

### Per-repository settings: `.activeloom-herd.json`

Commit it at the repository root. It is consumer-owned: sync never writes it. Every section is optional, and an absent section means disabled or the most conservative choice.

The skills read this file **from the integration branch at `origin`, never from a PR head**, the same rule `.activeloom-review.json` follows. A pull request cannot loosen its own gate-fix, glance or ship rules by editing the file; a change takes effect once it is merged. The helper reads the default branch first; if that copy names a different `integration_branch`, the copy on that branch is the one that counts, and it must name the same branch.

A complete example for a fictional `acme/widgets` that works on `staging` and promotes to `main`:

```json
{
  "schema_version": 1,
  "integration_branch": "staging",
  "worktree": { "bootstrap": [["pnpm", "install", "--frozen-lockfile"]] },
  "gate_fix": {
    "product_globs": ["apps/**", "packages/*/src/**"],
    "tooling_globs": ["scripts/**", ".github/scripts/**"],
    "test_globs": ["**/*.test.ts", "**/fixtures/**"],
    "off_limits_globs": [
      ".github/workflows/**",
      "infra/**",
      "**/package.json",
      "pnpm-lock.yaml"
    ],
    "extra_checks": [["pnpm", "db:check"]]
  },
  "glance": { "auto": true },
  "ship": {
    "on_ship": "label",
    "label": "status: on-staging",
    "checks": "all-runs",
    "post_ship_checks": []
  },
  "builds": { "engines": ["claude", "codex"] },
  "grill": {
    "engines": ["claude", "codex", "gemini"],
    "hold_label": "agent-bail: grill-review",
    "verify_first_label": null,
    "persona": "Product lead for a small team."
  },
  "production_lanes": [
    {
      "name": "docs-video",
      "engine": "codex",
      "skill": "record-walkthrough",
      "brief": "docs/video-plan.md"
    }
  ],
  "promotion": {
    "skill": "promote",
    "production_branch": "main",
    "risk_engine": "gemini",
    "preflight": [["pnpm", "typecheck"]],
    "deploy_workflows": ["Deploy"],
    "post_merge": []
  }
}
```

Every command is an argv array, never a shell string: `["pnpm", "install"]`, not `"pnpm install"`. The helper rejects a one-element command containing a space.

#### Section reference

| Key                        | Default when absent           | Meaning                                                                                                                                                                                                                                                                                                              |
| -------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`           | required, `1`                 | Unknown keys anywhere are errors, so a typo cannot silently disable a rule.                                                                                                                                                                                                                                          |
| `integration_branch`       | the remote's default branch   | Where converged work ships and where this file is read from.                                                                                                                                                                                                                                                         |
| `worktree.bootstrap`       | none                          | Commands run in each new worktree before a session starts in it.                                                                                                                                                                                                                                                     |
| `gate_fix`                 | absent: disabled              | When a pass is blocked by a red gate the PR did not cause, `hreview` may open a separate, small, test-or-tooling-only fix PR for you to merge. Paths are classified off-limits first, then test, product, tooling; a fix touching off-limits, product or unclassified paths is refused. There are no built-in globs. |
| `gate_fix.extra_checks`    | none                          | Run in the PR worktree after the fix is merged in, before the pass reruns.                                                                                                                                                                                                                                           |
| `glance.auto`              | `false`                       | When `true`, `hreview` runs `auto-glance` itself at the human-glance gate. When `false`, it stops for a human glance.                                                                                                                                                                                                |
| `ship`                     | absent: `horch` never merges  | Present: `horch` ships converged work to the integration branch through `ship-staging`.                                                                                                                                                                                                                              |
| `ship.on_ship`             | `none`                        | After the merge: `label` adds `ship.label` to linked open issues, `close` closes them with a comment naming the merge commit, `none` leaves them.                                                                                                                                                                    |
| `ship.checks`              | `all-runs`                    | `required`: the branch's required checks must pass, and an empty required set fails the gate. `all-runs`: required checks must pass and every workflow run at the head must have completed green, with at least one run.                                                                                             |
| `ship.post_ship_checks`    | none                          | Run in the primary checkout after a ship. A failure is reported; the PR stays shipped.                                                                                                                                                                                                                               |
| `builds.engines`           | the orchestrator's own engine | Engines `horch` may start builds on.                                                                                                                                                                                                                                                                                 |
| `grill.engines`            | `claude`, `codex`, `gemini`   | Grill chain order. Engines your review profile marks unavailable are skipped; fewer than two is a block.                                                                                                                                                                                                             |
| `grill.hold_label`         | `agent-bail: grill-review`    | Added when a chain finishes, so the issue waits for your check and stays out of the agent queues.                                                                                                                                                                                                                    |
| `grill.verify_first_label` | none                          | Removed after a chain, which counts as the verification it asked for.                                                                                                                                                                                                                                                |
| `grill.persona`            | "the issue owner"             | One line describing who the driver answers as.                                                                                                                                                                                                                                                                       |
| `production_lanes`         | none                          | Token-heavy production work (videos, long documents) that `horch` may hand to a named engine and skill. It stops at a local artifact for your approval.                                                                                                                                                              |
| `promotion`                | absent: `hpromote` refuses    | `skill` (your repository's own promotion skill) and `production_branch` are required. `risk_engine` defaults to the first engine in your deep review order that is not the driver's. `preflight`, `deploy_workflows` and `post_merge` feed the promotion phases.                                                     |

Validate a draft before you commit it:

```bash
python3 -I .codex/skills/hreview/scripts/herd-config.py validate .activeloom-herd.json
```

The JSON schemas ship beside the helper (`herd-config.schema.json`, `herd-user.schema.json`) for editor completion. The helper is the gate; it also rejects shell strings and escaping globs, which a schema cannot express.

## The skills, one by one

Examples use Claude Code's `/skill` form. In Codex and Gemini/Agy, name the skill instead: "use hreview on PR 42". See [driving from each harness](#driving-from-each-harness).

### `hreview <pr>` — live review chain

What you type: `/hreview 42`, or `/hreview 42 43 --companion` for PRs that must be reviewed in order. Add `--author codex` when the PR's commits do not name exactly one authoring engine.

What appears: the session you typed into becomes the **herder**. It splits one row below itself per PR, with one pane per reviewing engine in your review profile's order. Idle panes read `waiting`, the running pane `claude r1`, finished ones `done@<head>`.

What it does: resolves the tier and round cap from the review workflow, starts or resumes the PR's review run, posts the roster, then starts each engine's ordinary review skill in its pane, one at a time. Each pass reviews everything the earlier passes fixed. It answers the reviewers' prompts, accepts tier recommendations, restarts stalled sessions and relaunches crashed ones.

Where it stops:

- **Converged and CI green:** it marks the PR ready and tells you it is ready for final review and merge.
- **Converged, CI red:** the PR stays draft; you get the failing job and the same job's latest result on the base branch. It does not fix CI.
- **Out of rounds:** the run ends exhausted and the PR stays draft.
- **Glance-sized PR:** with `glance.auto: true`, it runs `auto-glance` in the same herder session and reports its result, forwarding the resolved author (`--author` takes precedence). With `false`, it stops for a human glance. Always-Deep surfaces and existing review runs stay on the chain path.
- **Blocked:** anything it cannot decide within the rules.

It never merges, marks a PR ready only through the converged path or the auto-glance clean-result and CI gate (plus the documented gate-fix exception), never starts a new run to get more rounds, and never overrides or dismisses a reviewer's finding.

### `auto-glance <pr>` — one model instead of a human glance

Only runs when the repository sets `glance.auto: true`. What you type: `/auto-glance 42`; `hreview` starts it directly at the glance gate, including when invoked without `horch`. The orchestrator watches that result instead of starting a second glancer.

It confirms the PR really is glance-sized and not on an always-Deep surface, picks the first non-author engine from your Lean order, and starts it in a pane below. The reviewer reads the whole diff and posts one comment, starting with an `auto-glance:v1` marker, that lists every finding with severity and confidence. If nothing is blocking or major and CI at that exact head is green, the PR is marked ready and an `auto-glance-result:v1` reply records the clean result. Otherwise the PR is left alone and you are alerted.

It never merges, never pushes to the PR (the reviewer may not either), and never drops or downgrades a finding.

### `hgrill <issue>` — automated grill chain

What you type: `/hgrill 17`, `/hgrill 17 18 --kind product-grill`, or `/hgrill next 3` for the top of the grill queue.

What appears: one `G<n>` tab per issue, at most three at once. In each, a driver session and a row of engine panes. Each engine runs the `grill` or `product-grill` skill on the issue in turn, with the driver answering as the issue owner: it accepts every recommendation, asks once per pass for the most controversial calls, and approves the handback.

After the last pass the driver itself reconciles the disputes (technical ones re-decided on merit, product ones left to you with a recommendation), checks the easy assumptions against the code and documentation, and runs a correction pass if one turns out to be false. It then puts an **assumptions block** at the top of the issue: the product calls the passes made for you, the assumptions they rest on, what was checked, and work owed to other issues. It adds the hold label so nothing picks the issue up until you have read it.

To sign off, tell any session you accept the block; it flips the callout and removes the hold. To reject calls, say which, and a correction run applies your decisions.

It touches only the chained issue and its children.

### `horch [instruction file]` — workspace orchestrator

What you type: `/horch ./push-notes.md` in a fresh session in the workspace you want orchestrated. The instruction file is yours and stays local: what this push covers, the order of work, PRs to hold back, and decisions you reserve. With no file, `horch` looks for its last handoff and otherwise asks you.

It inventories every tab and session in the workspace, checks your instruction file against GitHub, and then:

- starts an `hreview` chain in a new tab for each PR that is ready to review;
- starts builds you scheduled on the engines `builds.engines` allows; each ends at a draft PR;
- watches `hreview` dispatch and finish `auto-glance` for glance-sized PRs (or alerts you when `glance.auto` is off);
- dispatches `hgrill`, `hloop` and `hpromote` when you ask;
- holds: when you ask it to pause, it starts no new work until you lift the hold;
- hands production-lane work to the configured engine and queues the result for your approval;
- **ships** each converged chain or clean glance to the integration branch through `ship-staging`, after a fail-closed gate: converged at the exact head with coverage verified (or a clean glance), mergeable, required checks green, and, by default, every CI run at the head green. Shipped tabs stay open with a banner as the record.

It never ships to a production branch and never forces a gate. Anything that stops a ship or a chain marks the tab `⚠`, notifies you, and tells you what failed and what you need to decide. When you end the push it writes a handoff file in its state directory for the next session.

### `hloop next <k>` — batch loop

What you type: `/hloop next 5` for the top five agent-ready issues, `/hloop 17` for issue #17, or `/hloop 17 18 21` for several named issues; add `--concurrency <c>` to change how many builds run at once (default 3). It runs as the workspace's orchestrator and follows `horch` for everything else.

Before launching it screens the queue and shows you the plan: issues assigned to someone else, excluded by refinement or not yet refined are skipped with a reason; an issue that lists design options with no recorded choice goes to the grill queue; and issues whose bodies name the same files run one after the other, behaviour changes first, so the second PR does not conflict once the first ships.

Each issue then gets a build tab whose session opens a draft PR and starts no review, a fresh `hreview` chain (or `auto-glance` at the glance gate), the PR's own and changed tests run at its head, and a ship through `horch`'s gate. Deferred findings that are confirmed and user-visible become one follow-up issue per PR, labelled so the next batch skips it until it is refined.

To pause after the current work, tell the orchestrator. It writes a `HOLD` line to its state file and starts nothing new until you lift it; running chains finish and converged PRs still ship unless you say otherwise.

### `hpromote` — promotion

What you type: `/hpromote`, or `/hpromote --dry-run` to stop after the risk review. Requires a `promotion` section.

A dry run pins the integration and production SHAs and reports its verdict locally, even when no promotion PR exists. It creates no PR, posts no comments and starts no fixes. On completion it releases its lock, removes its clean review worktree and labels the tab `P<mmdd> ✓ dry-run`; findings stay in the report, and the label does not mean production was deployed or approved.

What appears: a `P<mmdd>` tab. The driver runs your repository's own promotion skill (named in `promotion.skill`), opens or adopts the promotion PR, and starts a **read-only** risk review of the whole diff in a `risk` pane. It verifies every blocker and major finding itself, posts one comment with dispositions, and for each confirmed blocker starts a fix session that opens a PR to the integration branch, gets it reviewed in a `review-<pr>` pane, and ships it through `horch`'s gate. When the integration branch moves, it reviews only the delta.

It stops at `P<mmdd> ⏸ approve`: approving and merging the promotion PR deploys production, and both are yours. After you merge, tell it `merged` (or tell `horch`), and it watches the deploy workflows and runs the post-merge checks. It never rolls back on its own.

## Driving from each harness

Any harness can be the herder, orchestrator or driver, and any engine can run in a pane.

| Harness     | Start a herd skill               | Alerts beyond herdr's notification | Long waits                                                             |
| ----------- | -------------------------------- | ---------------------------------- | ---------------------------------------------------------------------- |
| Claude Code | `/hreview 42`                    | A push notification, if enabled    | Waits run in the background; Claude resumes when they finish           |
| Codex       | "use the hreview skill on PR 42" | None                               | Holds a blocking `herdr agent wait` for several minutes, then re-waits |
| Gemini/Agy  | "use the hreview skill on PR 42" | None                               | Backgrounds the wait, ends its turn, and resumes when it completes     |

The skills use one waiting rule everywhere: run the wait, and if the harness backgrounds it, resume on completion. Sessions talk through herdr itself (`herdr agent prompt` to tell, `herdr agent read` to hear), so no harness needs a messaging feature the others lack. In Claude Code the herd skills start only when you invoke them.

Each engine launches with its model and effort from your review profile and its arguments from your `herd.json`. The shared mechanics, including how each engine is launched and how a finished session looks, are in `references/herdr.md` in each harness root.

## Troubleshooting

**"This session is not inside herdr."** Start the session from a herdr pane. `HERDR_ENV` is set only there.

**A pane never starts (`agent_pane_busy`).** A new tab's shell takes a few seconds to draw its prompt. The skills retry five times; if your shell prompt is slow (heavy rc files), open the tab, let it settle, and rerun.

**Startup dialog (`agent_not_ready`).** A folder-trust prompt on a new worktree, an update banner, or a first-run question. The driver trusts only the primary checkout and worktrees it created, and dismisses update banners. It never runs an update from a chain. Update your CLIs between runs.

**The Codex pane exits immediately.** Almost always a flag passed twice: a shell alias plus `permission_off_args`, or the same flag in both lists. Run `type codex`; remove the alias and keep the flag in `herd.json`.

**A pane says `blocked` but is working.** herdr's status can lag after a question is answered. The skills read the pane, not the status, and wait for `working`, `done` or `idle` after answering. If you answered a question by hand, the driver notices on its next read.

**Codex shows `idle` mid-task.** Codex settles at `idle`, never `done`, and also shows `idle` while waiting on its own background terminal. The skills treat a session as finished only when its pane shows the end-of-turn signature.

**Typed into a pane by accident.** That is the intended way to step in. The driver reads the pane before acting and continues from whatever state you left.

**A ghost suggestion in an input box.** Greyed text in an agent's input box is the client's autosuggestion. The driver never sends `tab` or `right` to a session, so it is never accepted.

**`herd-config.py` exits 1 or 2.** 1 is an invalid config (the message names every problem); 2 is a git problem, often a missing `origin/HEAD`. Pass `--default-branch <name>` or run `git remote set-head origin --auto`.

**A ship refuses.** Read the gate line in the alert. Common causes: a required check still pending, no required checks with `ship.checks: required`, a CI run still running, or `ship-staging` refusing on its own guardrails (it currently ships only to `staging` and needs its `status: on-staging` label and webhook). The PR is left as it was.

**A lock is held.** `hgrill` and `hpromote` lock per issue and per day. A block leaves the lock on purpose. Once you have dealt with the block, release it with `herd-config.py unlock` using the same `--repo`, `--skill` and `--key`.

## Adopting it in a repository

1. Install ActiveLoom with the harnesses you use plus Codex, and merge the sync PR that delivers the herd skills.
2. Install herdr 0.9.0 or later. Each person who drives sets up the review profile (`review-setup`) and, if they want unattended panes, `~/.config/activeloom/herd.json`.
3. Write `.activeloom-herd.json`. Start small: `schema_version`, `integration_branch` and `worktree.bootstrap`. That is enough for `hreview` and `hgrill`.
4. Validate it, open a PR, review and merge it. It counts only once it is on the integration branch.
5. Try `hreview` on one draft PR and watch the panes. Then add sections as you trust them: `gate_fix` once you know which paths are safe for a tooling-only fix, `glance.auto` if you want small PRs read by a model, `ship` when `horch` should merge into your integration branch, `promotion` when you have a promotion skill.
6. Keep the per-push instruction file for `horch` local; it is a working note, not repository policy.

The skills themselves are synced from ActiveLoom and overwritten on each sync. Change behaviour through the config files, or propose the change upstream.
