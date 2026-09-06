# activeloom

One agent toolkit for many repositories — code review, debugging, and issue skills for Claude Code, Codex, and Gemini, installed the same way everywhere.

```bash
npx activeloom add critique
```

That installs a skill into your own agent configuration. No repository, no account, no key, nothing committed.

## Four ways in

Each rung is the one below it plus exactly one thing, and the thing being added is **credential cost**. Start wherever you like; moving up later never means undoing what you already did.

| Tier                | Command                            | Needs        | You get                                                  |
| ------------------- | ---------------------------------- | ------------ | -------------------------------------------------------- |
| **0 — Try it**      | `npx activeloom add <skill>`       | nothing      | skills in your own agent, on this machine                |
| **1 — Commit it**   | `npx activeloom init`              | nothing      | the same skills checked into a repo, for your whole team |
| **2 — Automate it** | `npx activeloom init --sync`       | nothing      | ...kept up to date by a daily pull request               |
| **3 — Sign it**     | `npx activeloom init --sync --app` | a GitHub App | ...with GitHub-signed commits, and private upstreams     |

**Tier 2 is the default.** A GitHub App is only ever needed at Tier 3.

## Commands

```bash
npx activeloom add                   # list every skill available
npx activeloom add critique issues   # install several at once
npx activeloom init --sync           # wire a repo up, with automated updates
npx activeloom detect                # show what this repo looks like; write nothing
npx activeloom tiers                 # explain the ladder
```

### Options

| Flag                 | Meaning                                                        |
| -------------------- | -------------------------------------------------------------- |
| `--harness <id>`     | `claude`, `codex`, or `gemini`. Repeatable. Default: detected. |
| `--ref <ref>`        | upstream ref to install from (default: `sync-v2`)              |
| `--base-branch <b>`  | branch sync PRs land on (default: your `origin` HEAD)          |
| `--python <path>`    | interpreter for the sync engine (default: `python3`)           |
| `--consumer-dir <d>` | repository to write into (default: current directory)          |
| `--dry-run`          | report what would happen, write nothing                        |
| `--force`            | replace files that already exist                               |

## How it works

This package ships the **installer**, not the prompts. Content is fetched from a tag-pinned tarball of [`loomantix/activeloom`](https://github.com/loomantix/activeloom) at run time — by default the `sync-v2` tag, which is the same ref the CI sync workflow tracks. One gate for both doors, so the CLI and CI can never deliver different prompts.

`init` does not reimplement the sync: it invokes the upstream's own `sync-engine.py`, with the same arguments the consumer workflow uses. What you get locally is what CI would deliver, by construction rather than by coincidence.

## Requirements

- **Node 18.17+** for every command. This package has zero dependencies.
- **Python 3.9+ with PyYAML** for `init` only. `add` does not need it.

## Documentation

Full walkthrough: [`docs/getting-started.md`](https://github.com/loomantix/activeloom/blob/main/docs/getting-started.md).

Apache 2.0 + DCO.
