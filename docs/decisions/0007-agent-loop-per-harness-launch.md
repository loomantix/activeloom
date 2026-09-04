# 0007 — agent-loop is per-harness: three launch models, three supervision models

- Status: accepted
- Date: 2026-09-04

## Divergent files

The whole skill, in all three prompt roots — `<root>/skills/agent-loop/`:

| file                             | `.claude` | `.codex` | `.agents` |
| -------------------------------- | --------- | -------- | --------- |
| `scripts/agent-loop.sh`          | 2965      | 3705     | 3008      |
| `scripts/agent-loop-state.py`    | 475       | 529      | 466       |
| `scripts/config-doctor.py`       | 128       | 358      | 135       |
| `scripts/review-push.sh`         | 122       | 291      | 139       |
| `scripts/process-supervisor.py`  | —         | 209      | —         |
| `scripts/run-codex-review.sh`    | —         | 288      | —         |
| `scripts/run-agy-launch.sh`      | —         | —        | 42        |
| `scripts/run-agy-worker.sh`      | —         | —        | 33        |
| `scripts/run-agy-review.sh`      | —         | —        | 106       |
| `SKILL.md`                       | 273       | 306      | 247       |
| `agent-loop-instructions.md.tpl` | 176       | 70       | 70        |
| `agent-loop.config.template`     | 104       | 87       | 74        |

Every file diverges; five exist in one root only.

## The behavioural difference

`agent-loop` is not a prompt that happens to name a different CLI. It is a
supervisor that spawns a coding agent non-interactively, with that agent's own
permission model disabled, and then reads its output. The line that spawns the
worker is a different program with a different contract in each root:

- **Claude** — `claude --permission-mode bypassPermissions --print`. One
  process, invoked directly, no wrapper.
- **Codex** — `codex exec --dangerously-bypass-approvals-and-sandbox -C <worktree>`.
  The worktree is passed to the CLI rather than entered, and the run is watched
  by a `process-supervisor.py` that has no counterpart in the other roots.
- **Gemini** — never invoked directly. `run-agy-worker.sh` sources a shared
  `run-agy-launch.sh` and calls the CLI with `--effort`, `--mode accept-edits`,
  `--dangerously-skip-permissions`, `--disable-slash-commands`,
  `--output-format json`, and `--print-timeout`. The launchers are fail-closed
  by design: they pin the trusted surface they will run against and refuse
  otherwise.

`--disable-slash-commands` is the sharpest example of why this cannot be one
document. The Gemini harness cannot address a skill as a command from inside a
loop-spawned session, so `run-agy-review.sh` reads `deepcritique/SKILL.md` and
inlines its prose, where the other two roots pass `deepcritique <pr>` as a
command string. That is not vocabulary. It is a different mechanism for getting
a review to happen, and it changes what the surrounding script has to build,
pass, and parse.

The same split runs through the rest of the skill: `config-doctor.py` validates
a config whose accepted contract versions differ per harness, `review-push.sh`
attests a review head through a different review path, and the instruction
template a consumer receives differs in length by more than a factor of two
because the Claude template carries harness-specific hook guidance the others
have no hooks for.

## Why it stands

Three arguments, in order of weight.

**It is an authority boundary, not prose.** These scripts run an agent with its
sandbox off, in a loop, over a queue of issues, in consumer CI and on
developers' machines. A shared implementation would mean one file whose flags
are correct for one CLI and approximated for two others, and the failure mode
of an approximated permission flag is not a bad review — it is a worker running
with the wrong isolation. Each root's launch line should be readable, in that
root, as exactly what will run.

**The divergence is load-bearing hardening, independently earned.** The Gemini
launchers exist because that harness needed a fail-closed trusted-surface pin;
the Codex supervisor exists because that CLI needed out-of-process supervision.
Neither is a stale fork of the other. Collapsing them onto a common shape would
delete hardening that was added for a reason specific to one runtime.

**A skill this different has no shared source to recover.** The renderer is
variables-only and will stay that way: it has no conditional, and a skill whose
_structure_ differs between harnesses is per-harness by definition
(`docs/prompt-rendering.md`). Rendering `agent-loop` would require branching on
the harness in at least the launch, the supervision, the review invocation, and
the config contract — which is not rendering, it is a second implementation
language with worse tooling.

## What still ports across roots

The divergence is in the launch and supervision layer, not in the protocol. A
change to the queue contract (`dev: agent` selection, `agent-bail:*`
classification), to the one-worktree-one-draft-PR model, to review-round bounds,
or to the review-ledger attestation format is a protocol change and belongs in
all three roots — rewritten against each root's own launch model, never copied
across. This is the same rule `0006` states for the review chain, applied to the
loop that drives it.

A defect found in one root's copy is therefore worth checking in the other two
by hand. This record allowlists the divergence; it does not certify that every
line of it is intentional.
