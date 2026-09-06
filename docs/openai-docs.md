# OpenAI documentation setup

Pair OpenAI's [official OpenAI Docs skill](https://github.com/openai/skills/blob/main/skills/.curated/openai-docs/SKILL.md)
with its [documentation MCP server](https://developers.openai.com/learn/docs-mcp)
when working on OpenAI APIs or Codex configuration. The skill supplies the
lookup workflow; the server supplies current documentation.

## Configure Codex once per developer

Check whether `openai-docs` is already available in your session's skill list.
If absent, install it from the [OpenAI skills repository](https://github.com/openai/skills)
using your client's skill installer. Install the complete skill directory:
the entry point depends on references and helper scripts. Keep it maintained
by its upstream installer rather than copying its contents into ActiveLoom's
synced skill templates.

Check the existing MCP configuration with `codex mcp list`. If
`openaiDeveloperDocs` is absent, add it:

```sh
codex mcp add openaiDeveloperDocs --url https://developers.openai.com/mcp
```

The equivalent user configuration in `~/.codex/config.toml` is:

```toml
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
```

User configuration applies across local repositories. Use a trusted project's
`.codex/config.toml` only when project-specific settings are needed; inspect
existing overrides before adding another entry. See the official
[MCP configuration guide](https://developers.openai.com/codex/mcp).

## Repository guidance and verification

Keep durable usage guidance in the repository's owned `AGENTS.md`, for example:

> When a task needs OpenAI API or Codex facts, use `openai-docs` if available
> and follow its source routing. Otherwise search and fetch official docs
> through Docs MCP, with official OpenAI websites as the fallback. Cite the
> supporting page and preserve explicitly requested model targets.

Follow the installed skill's route for broad Codex questions, documentation
lookup, and model migration. Avoid encoding a particular model ID or copying
the upstream skill's evolving workflow into every repository.

Verify both layers: confirm the skill is available, then ask the agent to
search for Responses API documentation, fetch the relevant page, and cite it.
A configured server alone does not prove tools are available in an existing
session; start a new local session if the client has not loaded the changes.
If Docs MCP is unavailable or unhelpful, use official OpenAI documentation and
state any unresolved uncertainty.

The server provides read-only documentation access; it does not execute API
requests. Configure other clients separately using the official Docs MCP guide.

## Agy (Antigravity/Gemini)

Keep the documentation policy in the repository's `AGENTS.md` so Agy can use
it too. Agy's installed customization documentation describes directory-scoped
`AGENTS.md` and `GEMINI.md` discovery. An existing `GEMINI.md` can point to
`AGENTS.md` for the shared policy instead of duplicating it.

Codex's user configuration and system skills do not establish that Agy has
the same tools or skills available. Check the current Agy session separately.
The installed CLI's `agy mcp add --help` documents HTTP server registration:

```sh
agy mcp add --type http openaiDeveloperDocs https://developers.openai.com/mcp
```

Check `agy mcp list` before adding a server, and verify search and page fetch
in an Agy session afterward. If the tools or skill are unavailable, use the
official-documentation fallback in `AGENTS.md`. A missing tool during a lookup
does not by itself require changing client configuration.
