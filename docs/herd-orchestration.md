# Herd orchestration on herdr

The herd skills let one agent session drive other agent sessions in visible terminal panes. You watch every review pass, grill interview and build as it happens, and you can type into any pane to step in. The driving session carries the work to a defined stopping point without asking you, and it stops for you at the decisions that are yours: merging to production, product calls, and anything it cannot do safely.

They run on [herdr](https://herdr.dev), a terminal workspace manager for coding agents. Any of the three harnesses (Claude Code, Codex, Gemini/Agy) can drive, and any of them can be driven.

| Skill         | What it does                                                                                                          | Stops for you when                                         |
| ------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `hreview`     | Runs the ordinary review chain on a PR, one engine per pane, until it converges or runs out of rounds.                | It converged (merge is yours), or it is blocked            |
| `auto-glance` | Puts one non-author model in the seat of the human glance for small or docs-only PRs. Opt-in per repository.          | The read found something, or CI is red                     |
| `hgrill`      | Grills an issue on each configured engine in turn, then posts the biggest product calls and assumptions for a check.  | Always: the assumptions block waits for your sign-off      |
| `horch`       | Orchestrates a whole workspace: starts chains, builds, grills and promotions in tabs and ships converged work.        | A ship gate fails, a chain blocks, or a call is yours      |
| `hloop`       | Runs a batch of agent-ready issues: a build tab per issue, then a review chain, then a ship through `horch`'s gate.   | An issue's design is open, a gate fails, or a chain blocks |
| `hpromote`    | Runs your repository's promotion recipe across at most six visible panes, with a read-only risk review and fix lanes. | The promotion PR is ready: approving and merging it        |

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
      "permission_off_args": ["--permission-mode", "auto"]
    },
    "codex": { "launch_args": [], "permission_off_args": ["--approve-for-me"] },
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

Permission-off flags belong here, not in a shell alias. The skills cannot see an alias, and a flag passed twice makes the Codex CLI exit, so an alias plus this file breaks every Codex launch. The helper rejects a flag that appears in both lists of one engine. Without permission-off flags, panes stop at every permission prompt and the driver has to answer them. That is slow, and it can fail: the driver's own harness may refuse to approve another agent's command it cannot inspect, which blocks the run partway through.

Before its first launch, each herd skill runs a permissions preflight (`herd-config.py permissions`). For every engine the run will start in a session that edits, it tells you whether those panes will stop at prompts and offers settings to add here, narrowest first: Claude `--permission-mode auto` or Codex `--approve-for-me`, which end the prompts but keep a check, then the full bypass. It writes one only if you choose it. When flags are set, it names what each one turns off, such as approval prompts or the sandbox, and warns about any such flag in `launch_args`, where it reaches read-only sessions too. It makes no offer for an engine whose flags it cannot classify, since an added flag could repeat one of them. Read [Running unattended safely](#running-unattended-safely) before choosing.

The preflight reads this file only. Settings in an engine's own config, such as `approval_policy` or `sandbox_mode` in `~/.codex/config.toml` or `permissions.defaultMode` in Claude settings, apply to herd panes too and do not show in its report. Writing this file only on your choice is a rule the skills follow; no code enforces it.

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

| Key                        | Default when absent           | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`           | required, `1`                 | Unknown keys anywhere are errors, so a typo cannot silently disable a rule.                                                                                                                                                                                                                                                                                                                                                        |
| `integration_branch`       | the remote's default branch   | Where converged work ships and where this file is read from.                                                                                                                                                                                                                                                                                                                                                                       |
| `worktree.bootstrap`       | none                          | Commands run in each new worktree before a session starts in it.                                                                                                                                                                                                                                                                                                                                                                   |
| `gate_fix`                 | absent: disabled              | When a pass is blocked by a red gate the PR did not cause, `hreview` may open a separate, small, test-or-tooling-only fix PR for you to merge. Paths are classified off-limits first, then test, product, tooling; a fix touching off-limits, product or unclassified paths is refused. There are no built-in globs.                                                                                                               |
| `gate_fix.extra_checks`    | none                          | Run in the PR worktree after the fix is merged in, before the pass reruns.                                                                                                                                                                                                                                                                                                                                                         |
| `glance.auto`              | `false`                       | When `true`, `hreview` runs `auto-glance` itself at the human-glance gate. When `false`, it stops for a human glance.                                                                                                                                                                                                                                                                                                              |
| `ship`                     | absent: `horch` never merges  | Present: `horch` ships converged work to the integration branch through `ship-staging`.                                                                                                                                                                                                                                                                                                                                            |
| `ship.on_ship`             | `none`                        | After the merge: `label` adds `ship.label` to linked open issues, `close` closes them with a comment naming the merge commit, `none` leaves them.                                                                                                                                                                                                                                                                                  |
| `ship.checks`              | `all-runs`                    | `required`: the branch's required checks must pass, and an empty required set fails the gate. `all-runs`: required checks must pass and every workflow run at the head must have completed green, with at least one run.                                                                                                                                                                                                           |
| `ship.post_ship_checks`    | none                          | Run in the primary checkout after a ship. A failure is reported; the PR stays shipped.                                                                                                                                                                                                                                                                                                                                             |
| `builds.engines`           | the orchestrator's own engine | Engines `horch` may start builds on.                                                                                                                                                                                                                                                                                                                                                                                               |
| `grill.engines`            | `claude`, `codex`, `gemini`   | Grill chain order. Engines your review profile marks unavailable are skipped; fewer than two is a block.                                                                                                                                                                                                                                                                                                                           |
| `grill.hold_label`         | `agent-bail: grill-review`    | Added when a chain finishes, so the issue waits for your check and stays out of the agent queues.                                                                                                                                                                                                                                                                                                                                  |
| `grill.verify_first_label` | none                          | Removed after a chain, which counts as the verification it asked for.                                                                                                                                                                                                                                                                                                                                                              |
| `grill.persona`            | "the issue owner"             | One line describing who the driver answers as.                                                                                                                                                                                                                                                                                                                                                                                     |
| `production_lanes`         | none                          | Token-heavy production work (videos, long documents) that `horch` may hand to a named engine and skill. It stops at a local artifact for your approval.                                                                                                                                                                                                                                                                            |
| `promotion`                | absent: `hpromote` refuses    | `production_branch` is required. `skill` defaults to the generic `promote`; name your own skill to override its step rules (keep `skill` explicit while older checkouts, which still require it, read this file). `risk_engine` defaults to the first engine in your deep review order that is not the driver's. Without a [promotion recipe](#the-promotion-recipe), `preflight`, `deploy_workflows` and `post_merge` become one. |

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
- **Run already ended:** a PR whose latest review run converged, exhausted or aborted needs a restart you name in the herder's own tab. It stops with a `RESTART NEEDS YOU` block giving the words to type, `/hreview 42 --restart`, plus `--scope-decision keep|split` when the PR has six or more feat/fix commits and the run controller will ask for one. Add `--deep` to ask for a Deep review. A restart relayed by the orchestrator or another session does not count, and the block says so.
- **Blocked:** anything it cannot decide within the rules.

It never merges, marks a PR ready only through the converged path or the auto-glance clean-result and CI gate (plus the documented gate-fix exception), never starts a new run to get more rounds, and never overrides or dismisses a reviewer's finding.

### `auto-glance <pr>` — one model instead of a human glance

Only runs when the repository sets `glance.auto: true`. What you type: `/auto-glance 42`; `hreview` starts it directly at the glance gate, including when invoked without `horch`. The orchestrator watches that result instead of starting a second glancer.

It confirms the PR really is glance-sized and not on an always-Deep surface, picks the first non-author engine from your Lean order, and starts it in a pane below. The reviewer runs as a read-only session, without your permission-off flags, and reads the whole diff. It returns one comment, starting with an `auto-glance:v1` marker, that lists every finding with severity and confidence, and the skill posts it. If nothing is blocking or major and CI at that exact head is green, the PR is marked ready and an `auto-glance-result:v1` reply records the clean result. Otherwise the PR is left alone and you are alerted.

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

Before starting a chain it reads the PR's run state (`local-review-handoff.py run-state`). For a PR whose run has already ended it starts no herder, because only your words in the herder's tab can authorize a restart. It alerts you instead with the exact words to type into a fresh session in a new tab, including the scope decision when the run controller will ask for one, and adopts that tab once the run starts.

It never ships to a production branch and never forces a gate. Anything that stops a ship or a chain marks the tab `⚠`, notifies you, and tells you what failed and what you need to decide. When you end the push it writes a handoff file in its state directory for the next session.

### `hloop next <k>` — batch loop

What you type: `/hloop next 5` for the top five agent-ready issues, `/hloop 17` for issue #17, or `/hloop 17 18 21` for several named issues; add `--concurrency <c>` to change how many builds run at once (default 3). It runs as the workspace's orchestrator and follows `horch` for everything else.

Before launching it screens the queue and shows you the plan: issues assigned to someone else, excluded by refinement or not yet refined are skipped with a reason; an issue that lists design options with no recorded choice goes to the grill queue; and issues whose bodies name the same files run one after the other, behaviour changes first, so the second PR does not conflict once the first ships.

Each issue then gets a build tab whose session opens a draft PR and starts no review, a fresh `hreview` chain (or `auto-glance` at the glance gate), the PR's own and changed tests run at its head, and a ship through `horch`'s gate. Deferred findings that are confirmed and user-visible become one follow-up issue per PR, labelled so the next batch skips it until it is refined.

To pause after the current work, tell the orchestrator. It writes a `HOLD` line to its state file and starts nothing new until you lift it; running chains finish and converged PRs still ship unless you say otherwise.

### `hpromote` — promotion

What you type: `/hpromote`, or `/hpromote --dry-run` to stop after the risk review. Requires a `promotion` section.

A dry run pins the integration and production SHAs and reports its verdict locally, even when no promotion PR exists. It creates no PR, posts no comments and starts no fixes. On completion it releases its lock, removes its clean review worktree and labels the tab `P<mmdd> ✓ dry-run`; findings stay in the report, and the label does not mean production was deployed or approved.

What appears: a `P<mmdd>` tab of at most six panes, the driver on top and up to five worker slots below it. The driver reads your [promotion recipe](#the-promotion-recipe) and runs each phase's lanes in the slots, so every preflight command, CI watch and deploy watch runs in a pane you can see. It opens or adopts the promotion PR and runs the recipe's **read-only** risk review of the whole diff in its own slot. It verifies every blocker and major finding itself, posts one comment with dispositions, and for each confirmed blocker starts a fix session that opens a PR to the integration branch, gets it reviewed in a `review-<pr>` pane, and ships it through `horch`'s gate. When the integration branch moves, it reviews only the delta.

It stops at `P<mmdd> ⏸ approve`: approving and merging the promotion PR deploys production, and both are yours. After you merge, tell it `merged` (or tell `horch`), and it runs the recipe's post-merge lanes: deploy watchers and checks side by side. It never rolls back on its own.

#### The promotion recipe

`.activeloom-promote.json`, committed on your integration branch and read from the remote like `.activeloom-herd.json`, says exactly what a promotion is for your repository. The `promote` skill runs it in one session without herdr; `hpromote` runs it across panes. Nothing about any one repository's process is built in.

For the first standalone promotion when the remote default is production and has no recipe yet, use `/promote --integration-branch develop` (substitute your integration branch). This reads the committed remote recipe directly. Once the default branch carries a recipe naming the integration branch, `/promote` discovers it without the option. `hpromote` can discover the integration branch from its herd configuration.

Sync bootstraps `promote/SKILL.md` only when absent, preserving existing repository-owned promotion instructions. The skill body remains consumer-owned after bootstrap; review and apply later upstream skill changes manually. Its recipe helper and schema continue to sync normally.

Legacy herd configs without a recipe retain deployment-before-verification ordering: synthesized post-merge workflow watches and commands share one ordered lane. Explicit recipes can use separate lanes for independent checks, or put dependent smoke commands after their deployment watch in the same lane.

```json
{
  "schema_version": 1,
  "integration_branch": "develop",
  "production_branch": "main",
  "guide": "docs/release/promotion.md",
  "rollback": "docs/runbooks/rollback.md",
  "blocker_issues": { "labels": ["type: bug"] },
  "pull_request": {
    "merge_method": "merge",
    "surface": [{ "name": "migrations", "globs": ["db/migrations/**"] }],
    "after_merge": ["Confirm the release notes page shows this promotion."]
  },
  "steps": [
    {
      "id": "install",
      "kind": "command",
      "phase": "preflight",
      "run": ["npm", "ci"],
      "fix": ["npm", "install"]
    },
    {
      "id": "lint",
      "kind": "command",
      "phase": "preflight",
      "run": ["npm", "run", "lint"]
    },
    {
      "id": "release-gate",
      "kind": "command",
      "phase": "preflight",
      "lane": "gate",
      "run": ["scripts/release-gate"],
      "on_fail": "hold"
    },
    {
      "id": "deps",
      "kind": "task",
      "phase": "preflight",
      "brief": "docs/release/dependency-prs.md",
      "writes": true
    },
    {
      "id": "integration-deploy",
      "kind": "watch_workflow",
      "phase": "preflight",
      "workflow": "deploy.yml"
    },
    {
      "id": "risk",
      "kind": "risk_review",
      "phase": "pre_merge",
      "focus": ["Background job retries"]
    },
    { "id": "ci", "kind": "watch_checks", "phase": "pre_merge" },
    {
      "id": "deploy-app",
      "kind": "watch_workflow",
      "phase": "post_merge",
      "workflow": "deploy.yml"
    },
    {
      "id": "smoke",
      "kind": "command",
      "phase": "post_merge",
      "lane": "verify",
      "run": ["scripts/smoke", "production"]
    }
  ]
}
```

Phases run in order: `preflight` on the integration head, then the promotion PR is opened, `pre_merge` on its head, your approval and merge, and `post_merge` on the merge commit. Inside a phase, steps that share a `lane` run in order and stop at the first failure; different lanes are independent. A `command` or `task` defaults to the shared `main` lane (a task to a lane of its own); each watcher and the risk review defaults to a lane of its own.

| Kind             | Runs                                                                         | Options                                                                                                                                               |
| ---------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `command`        | An argv in a worktree detached at the phase's commit                         | `fix`: a deterministic fix shipped as a PR to the integration branch. `on_fail`: `block` (default), `hold` (stops for you), `warn`. `timeout_minutes` |
| `task`           | A session that follows a brief in your repository, such as dependency triage | `writes`: whether it may act on what the brief says (default `false`; never in a dry run)                                                             |
| `risk_review`    | The read-only whole-diff review, `pre_merge` only                            | `engine`, `focus` items added to the review                                                                                                           |
| `watch_checks`   | The promotion PR's checks, `pre_merge` only                                  | `checks`: `required` or `all`. `rerun_flakes` (0-2, default 1). `timeout_minutes`                                                                     |
| `watch_workflow` | The newest run of a workflow on the phase's commit                           | `if_absent`: `fail` (default) or `skip` for path-filtered workflows. `timeout_minutes`                                                                |

How `hpromote` lays the tab out: `promote-recipe.py layout` maps each phase's lanes onto at most five slots, keeping one free for fixes before the merge. Lanes sit side by side; each watcher and the risk review keeps its own slot; when there are more lanes than slots, short lanes fold into one slot and run one after another. Slots are reused from phase to phase. The example above gives four preflight slots, two pre-merge slots and two post-merge slots.

Check a draft before you commit it:

```bash
python3 -I .codex/skills/promote/scripts/promote-recipe.py validate .activeloom-promote.json
python3 -I .codex/skills/promote/scripts/promote-recipe.py layout --recipe .activeloom-promote.json
```

## Running unattended safely

Herd sessions read pull request diffs, issue bodies and comments. In a public repository anyone can write those, and an instruction hidden in them is aimed at the agent that reads it (prompt injection). The same sessions hold an authenticated `gh` and can push. With approval prompts on, you see each command before it runs. Each permission setting removes some of that:

| Setting                                                                                | What a planted instruction can do                                                                         |
| -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| None (the default)                                                                     | Nothing you do not approve; panes stop at every prompt                                                    |
| Claude `--permission-mode auto`, Codex `--approve-for-me`                              | What the classifier or automatic reviewer lets through; Codex commands stay in its sandbox                |
| Claude or Agy `--dangerously-skip-permissions`, Codex `-a never -s danger-full-access` | Anything your user account can do: run commands, read files, use your `gh` token, push, reach the network |

Before you turn on the full bypass:

- Run the herd in a container, a VM, or under a dedicated OS user that holds only what the run needs.
- Give that environment a `gh` token scoped to the repositories the run works on, not your everyday login.
- Do not run bypassed sessions on pull requests from forks, or on issues from people outside your team.

Read-only sessions (the promotion risk review, the glance reviewer) never get your permission-off flags. They are the sessions most exposed to outside content, and they need no edits.

The herd skills are not the only place ActiveLoom starts an agent with checks off. `agent-loop`'s built-in workers launch Claude with `--permission-mode bypassPermissions` and Codex with `--dangerously-bypass-approvals-and-sandbox` ([ADR 0007](decisions/0007-agent-loop-per-harness-launch.md)); `herd.json` does not affect them, and the same advice applies.

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

**A chain blocks at an approval prompt in an engine pane.** The driver tried to answer the prompt and its own harness refused. Add one of that engine's permission settings to `herd.json` (the permissions preflight offers them, narrowest first), or answer the prompt in the pane yourself and tell the driver to continue.

**A chain stalls on refused commands.** A narrower setting (Claude `auto`, Codex `--approve-for-me`) refused something the pass needs, often `git push` or a `gh` call. Answer it in the pane, or move to the full bypass for that engine, in an isolated environment (see [Running unattended safely](#running-unattended-safely)).

**A pane says `blocked` but is working.** herdr's status can lag after a question is answered. The skills read the pane, not the status, and wait for `working`, `done` or `idle` after answering. If you answered a question by hand, the driver notices on its next read.

**Codex shows `idle` mid-task.** Codex settles at `idle`, never `done`, and also shows `idle` while waiting on its own background terminal. The skills treat a session as finished only when its pane shows the end-of-turn signature.

**Typed into a pane by accident.** That is the intended way to step in. The driver reads the pane before acting and continues from whatever state you left.

**A ghost suggestion in an input box.** Greyed text in an agent's input box is the client's autosuggestion. The driver never sends `tab` or `right` to a session, so it is never accepted.

**`herd-config.py` exits 1 or 2.** 1 is an invalid config (the message names every problem); 2 is a git problem, often a missing `origin/HEAD`. Pass `--default-branch <name>` or run `git remote set-head origin --auto`.

**A ship refuses.** Read the gate line in the alert. Common causes: a required check still pending, no required checks with `ship.checks: required`, a CI run still running, or `ship-staging` refusing on its own guardrails (it currently ships only to `staging` and needs its `status: on-staging` label and webhook). The PR is left as it was.

**A review run ended and the orchestrator will not restart it.** By design. Open a new tab in the workspace, start a session, and type the words the alert gave, such as `/hreview 42 --restart --scope-decision keep`. The orchestrator cannot pass your approval on for you.

**A lock is held.** `hgrill` and `hpromote` lock per issue and per day. A block leaves the lock on purpose. Once you have dealt with the block, release it with `herd-config.py unlock` using the same `--repo`, `--skill` and `--key`.

## Adopting it in a repository

1. Install ActiveLoom with the harnesses you use plus Codex, and merge the sync PR that delivers the herd skills.
2. Install herdr 0.9.0 or later. Each person who drives sets up the review profile (`review-setup`) and, if they want unattended panes, `~/.config/activeloom/herd.json`.
3. Write `.activeloom-herd.json`. Start small: `schema_version`, `integration_branch` and `worktree.bootstrap`. That is enough for `hreview` and `hgrill`.
4. Validate it, open a PR, review and merge it. It counts only once it is on the integration branch.
5. Try `hreview` on one draft PR and watch the panes. Then add sections as you trust them: `gate_fix` once you know which paths are safe for a tooling-only fix, `glance.auto` if you want small PRs read by a model, `ship` when `horch` should merge into your integration branch, `promotion` when you have a promotion skill.
6. Keep the per-push instruction file for `horch` local; it is a working note, not repository policy.

The skills themselves are synced from ActiveLoom and overwritten on each sync. Change behaviour through the config files, or propose the change upstream.
