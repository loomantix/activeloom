# GEMINI.md — Antigravity & Gemini Guide

Guidelines for Google Antigravity and Gemini CLI agents working in `gemini-platform`.

## Operational Defaults

- Always ensure commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`) and include DCO sign-off (`Signed-off-by:`).
- Preserve open-source clean-room discipline: zero internal company names, infrastructure endpoints, or customer references.
- All skills must define valid YAML frontmatter with `name` and `description` (using third-person framing).
- Keep skill instructions modular, actionable, and progressive.
- Run tests via `pytest` before submitting changes.
