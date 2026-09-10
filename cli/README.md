# ActiveLoom CLI

Install ActiveLoom engineering skills and supporting files for Claude Code, Codex, and Gemini/Agy, personally or into a repository. This CLI handles installation and detection; the agent clients execute the installed workflows.

For what ActiveLoom is and is not, read the [project README](https://github.com/loomantix/activeloom/blob/main/README.md). To route an agent task, use the [agent entry guide](https://github.com/loomantix/activeloom/blob/main/docs/agent-guide.md).

## Run from source

The CLI runs straight from a source checkout — no publication and no global install required:

```bash
git clone https://github.com/loomantix/activeloom.git
cd activeloom
node cli/bin/activeloom.js tiers
node cli/bin/activeloom.js add --harness codex
node cli/bin/activeloom.js add diagnosing-bugs --harness codex --dry-run
```

Remove `--dry-run` to install. After publication, `npx activeloom` will be the equivalent package invocation. The examples below assume you are in the source checkout; use `--consumer-dir` to target another repository.

## Choose the installation scope

| Tier                | CLI arguments                      | Effect                                                                                   |
| ------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------- |
| 0 — Personal        | `add <skill> --harness <id>`       | Copies selected skills and supporting files into personal harness configuration          |
| 1 — Repository      | `init --harness <id>`              | Writes selected harness trees, shared targets, and consumer config for review and commit |
| 2 — Scheduled sync  | `init --sync --harness <id>`       | Also generates a workflow proposing upstream updates with the built-in GitHub token      |
| 3 — App-backed sync | `init --sync --app --harness <id>` | Uses a configured GitHub App for GitHub-signed sync commits                              |

Tier 2 requires repository Actions permissions; Tier 3 requires App setup and separate read access for private upstreams. None installs agent clients or provides model access. These adoption tiers are independent of Lean/Deep review tiers.

## Commands

```bash
node cli/bin/activeloom.js add --harness claude
node cli/bin/activeloom.js add diagnosing-bugs issues --harness codex --dry-run
node cli/bin/activeloom.js detect --consumer-dir /path/to/your-repo
node cli/bin/activeloom.js init --harness codex --consumer-dir /path/to/your-repo --dry-run
node cli/bin/activeloom.js init --sync --harness codex --consumer-dir /path/to/your-repo
```

`detect` reports facts without installing files. `init` leaves `TODO(activeloom)` markers where project judgment is needed; run the `onboard` skill in your agent to draft those values for confirmation. On a fresh consumer, `init --dry-run` cannot render the complete harness trees until its configuration exists and reports the skipped render.

### Options

| Flag                 | Meaning                                                                                                          |
| -------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `--harness <id>`     | `claude`, `codex`, or `gemini` (Agy). Repeatable; default is detected.                                           |
| `--ref <ref>`        | Upstream content ref; default `sync-v2`. A SHA fixes a revision; a branch or tag may move.                       |
| `--upstream-dir <d>` | Install from a local checkout instead of downloading content.                                                    |
| `--base-branch <b>`  | Branch sync PRs target; defaults to detected origin HEAD.                                                        |
| `--python <path>`    | Python interpreter for `init`; default `python3`.                                                                |
| `--consumer-dir <d>` | Consumer repository; default current directory.                                                                  |
| `--yes`, `-y`        | Accept detected harnesses without prompting.                                                                     |
| `--dry-run`          | Preview without installing consumer or personal files.                                                           |
| `--force`            | Replace existing personal files or a differing generated sync workflow; `init` preserves consumer configuration. |

Options apply to the relevant command; invalid combinations are rejected. Select harnesses explicitly when preparing a shared repository setup. Installing one review skill does not provision a complete multi-engine review roster.

## Content and updates

The package contains the installer, not the prompt payload. By default it downloads a tarball of [`loomantix/activeloom`](https://github.com/loomantix/activeloom) at `sync-v2`. `init` invokes that upstream's `sync-engine.py` against the consumer configuration.

CLI installation and scheduled sync default to the same distribution ref, but installation time, harness selection, substitutions, and explicit refs affect the result. `sync-v2` is a movable distribution gate. Installer version, prompt-stack version, and installed revision are distinct.

`init --sync --ref <ref>` records the selected remote ref in the generated workflow. With `--upstream-dir`, the local install uses that checkout while the workflow retains its template ref; local edits are not published for subsequent syncs.

## Requirements and further setup

- Node 18.17+ for the CLI; it declares zero npm runtime dependencies.
- Python 3.9+ with PyYAML for `init`; `add` does not need Python.
- Network access for remote content; use `--upstream-dir` for an existing local payload.
- Separately installed agent clients and the tools/authentication required by each skill.

See [Getting started](https://github.com/loomantix/activeloom/blob/main/docs/getting-started.md) for config, Actions permissions, App setup, migration, and troubleshooting. Apache 2.0 + DCO.
