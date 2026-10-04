# Repository-owned internal documentation

`internal-docs` uses one rendered workflow for Claude Code, Codex and Gemini/Agy.
Consumers supply `internal-docs.config.json` with `schemaVersion: 1`, their canonical
`repository`, a command argument array and a repository-relative `setup` guide.
That command implements `doctor`, `find`, `read`, `create`, `update`, `validate`,
`prepare`, `preview` and `status`. Company registry, source mappings, metadata,
Markdown transforms and hosting remain consumer-owned.

## Install and upgrade

From a reviewed upstream checkout, preview then install:

```bash
node cli/bin/activeloom.js add internal-docs --harness claude --harness codex --harness gemini --upstream-dir . --dry-run
node cli/bin/activeloom.js add internal-docs --harness claude --harness codex --harness gemini --upstream-dir .
```

`add` is a personal install: it writes to the harness directories under your
home directory, and alongside the skill it installs that harness's shared support
files. `--force` replaces those support files as well as the skill. A dry run
without `--force` skips whatever already exists, so before an upgrade inspect the
list from `--dry-run --force`.

For repository delivery, use the existing `init`/sync path with those harnesses
selected in consumer configuration. The manifest carries all three skill files;
consumer destination gates must permit them. Upgrade through a reviewed sync PR
or `add --force` after inspecting the replacement. Record the exact upstream
revision with `git rev-parse HEAD` and the installed skill's SHA-256; a movable
sync ref is not an installed version.

Claude Code loads `.claude/skills/internal-docs/SKILL.md` and supports
`/internal-docs`. Current Codex native discovery uses `.agents/skills`, so select
the Gemini surface as well as Codex for this skill's native repository/personal
discovery; `.codex/skills` remains the legacy distributed/manual adapter. Through
sync, naming `gemini` delivers that harness's whole target set, not only this
skill: narrow it with `skip_targets` (see [Sync](sync.md)), or install this one
skill personally with `add --harness gemini`. Gemini
CLI documents `.agents/skills` as a workspace/user alias. Agy runtime behavior
needs an actual fresh-session smoke, independently of Gemini CLI documentation.
Codex-only legacy installation can read its installed file explicitly and use
the manual command contract; it is not claimed as native discovery.

Sources: [OpenAI skills](https://developers.openai.com/codex/skills),
[Claude Code skills](https://code.claude.com/docs/en/skills),
[Gemini CLI skills](https://geminicli.com/docs/cli/skills/).

## Diagnose and exercise

Start a fresh client in a disposable synthetic consumer. Confirm native discovery
or state that you explicitly loaded the skill file. Invoke the configured command
with an explicit mapping of two synthetic repository identities to local Git
checkouts. Exercise find, read, create, update, immutable preparation and local
draft preview. Check that the same update retains its stable ID/route. Record
client version, installed file digest, commands, actual tool outcomes and any
unavailable tools. Static parity/installer tests are not runtime smoke evidence.

A repository without `internal-docs.config.json` has not adopted the workflow,
and the skill stands aside for ordinary editing. Where the file exists, missing
source access or command executables is a local setup failure. Reader browser
sign-in does not grant CLI access. Missing hosting credentials does not block
local authoring. A prepared artifact supplies no verified live URL. Publication,
protected hosted previews, static downloads and interactive isolation require
their consumer integrations and separate review.

Clients without native skills but with a shell can read the installed `SKILL.md`
explicitly and invoke the command array. Clients without a shell use the
consumer's documented manual equivalent; no hosted vendor Artifact is required.
