# claude-platform — Claude project guide

Upstream source-of-truth for Loomantix's Claude Code skills, agents, and sync engine. Apache 2.0 + DCO. See [README.md](README.md) for what ships here and [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Public-Repo Policy

This repo is public. Keep repository content suitable for public readers:

- Do not reference non-public repositories, systems, incidents, or trackers by name.
- Do not document deployment-specific wiring, app slugs, secret names, or escalation paths beyond the public templates.
- Keep compliance and security rationale generic; do not include organization-specific evidence or customer-specific details.
- Put project-specific consumer details in that consumer's own repository, not here.

If work needs non-public context, discuss that context outside this public repository and keep any public issue or PR focused on the reusable change.

## Everything under `.claude/` is a prompt

Skills, agent definitions, and the instruction strings a skill tells Claude to pass to `Agent(...)` are all read by the model as instructions — so they are version-sensitive in a way ordinary docs are not. A phrasing that improved results on one model generation can suppress findings or waste tokens on the next.

**Read [.claude/MODEL_NOTES.md](.claude/MODEL_NOTES.md) before editing anything under `.claude/skills/` or `.claude/agents/`,** and work through its checklist before opening the PR. The short version: review agents report everything and the caller filters, no self-verification scaffolding, bounded subagent delegation, explicit output-length ceilings on every `Agent(...)` prompt, and no generic model-behavior boilerplate the harness already supplies.

## Cross-references

- [README.md](README.md) — what ships here, how to install skills, how to wire up a consumer.
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, scope, branch / commit / DCO conventions.
- [SECURITY.md](SECURITY.md) — responsible disclosure.
- [.claude/REVIEW_WORKFLOW.md](.claude/REVIEW_WORKFLOW.md) — canonical AI review chain.
- [.claude/MODEL_NOTES.md](.claude/MODEL_NOTES.md) — prompt-authoring deltas for the current default model.
