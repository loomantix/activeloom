# claude-platform — Codex Project Guide

Upstream source of truth for Loomantix's Claude Code skills, agents, and sync engine. Apache 2.0 + DCO. See [README.md](README.md) for what ships here and [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Public-Repo Policy

This repo is public. Keep repository content suitable for public readers:

- Do not reference non-public repositories, systems, incidents, or trackers by name.
- Do not document deployment-specific wiring, app slugs, secret names, or escalation paths beyond the public templates.
- Keep compliance and security rationale generic; do not include organization-specific evidence or customer-specific details.
- Put project-specific consumer details in that consumer's own repository, not here.

If work needs non-public context, discuss that context outside this public repository and keep any public issue or PR focused on the reusable change.

## Working Rules

- Start each session by reading this file and checking `git status --short --branch`.
- Use `rg` / `rg --files` for search and file discovery.
- Use `apply_patch` for manual file edits where practical.
- Do not revert user changes or unrelated dirty worktree state.
- Keep changes scoped to the user's request and the existing repo architecture.
- Run the smallest meaningful validation command after edits; report anything that could not be run.
- Read [.claude/MODEL_NOTES.md](.claude/MODEL_NOTES.md) before editing anything under `.claude/skills/` or `.claude/agents/` — those files are prompts, and the notes record the model-generation deltas that make some plausible-looking phrasings harmful.
- Read [.claude/SKILL_AUTHORING.md](.claude/SKILL_AUTHORING.md) alongside it when adding or restructuring a skill — it covers document structure (invocation, information hierarchy, completion criteria, pruning) where MODEL_NOTES covers model-generation deltas.

## OpenAI documentation (Codex and Agy)

When a task needs facts about OpenAI products or APIs, including Codex
configuration, use current official OpenAI documentation. This applies to
both Codex and Agy (Antigravity/Gemini).

- If `openai-docs` is available in the current client, use it and follow its
  source routing. Do not assume another client's skills or global config apply.
- Otherwise, use the OpenAI documentation MCP tools when available: search for
  the topic, then fetch the relevant page. If unavailable or unhelpful, search
  and open official pages on `developers.openai.com`, `platform.openai.com`,
  or `learn.chatgpt.com`.
- Cite supporting pages; state uncertainty when the sources do not establish
  the answer. Preserve explicitly requested model targets and existing
  provider choices unless the task authorizes a change.
- Keep documentation queries generic; never send secrets, personal data, or
  private repository content to documentation tools or web search.

See [OpenAI documentation setup](docs/openai-docs.md).

## Cross-References

- [README.md](README.md) — what ships here, how to install skills, how to wire up a consumer.
- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, scope, branch / commit / DCO conventions.
- [SECURITY.md](SECURITY.md) — responsible disclosure.
- [.claude/REVIEW_WORKFLOW.md](.claude/REVIEW_WORKFLOW.md) — canonical AI review chain.
- [.claude/MODEL_NOTES.md](.claude/MODEL_NOTES.md) — prompt-authoring deltas for the current default model.
- [.claude/SKILL_AUTHORING.md](.claude/SKILL_AUTHORING.md) — how to structure a skill document: invocation, information hierarchy, completion criteria, pruning.
