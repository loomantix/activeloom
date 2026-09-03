# gemini-platform — Agent Guide

Upstream source of truth for Google Antigravity / Gemini CLI skills, rules, and repo-sync automation. Apache 2.0 + DCO.

## Repository Policy

This repo is public-facing. Keep all issues, PRs, comments, and docs suitable for public readers:

- Do not reference private consumer repositories by name.
- Do not document private fleet topology, internal escalation paths, or deployment-specific secret names.
- Keep compliance rationale generic; do not describe private audit findings or control mappings.
- Put consumer-specific details in the consumer repo, not here.

## Working Rules

- At the start of work in this repo, verify local skill bootstrap with `./scripts/install-skills.sh --dry-run`. If it reports missing skills, run `./scripts/install-skills.sh` before relying on commands such as `critique`, `deepcritique`, `reviewit`, or `agent-loop`.
- Preserve the engine-agnostic sync model unless an Antigravity feature genuinely requires a schema change.
- Do not do implementation work directly on `main`; create a topic branch and PR back to `main`.
- Put Antigravity-discoverable workflows under `.agents/skills/<name>/SKILL.md`.
- Keep rules and guidelines under `.agents/rules/<name>.md`.
- Keep large or optional role prompts under `.agents/references/` (or within skills) and have skills load them on-demand using progressive disclosure.
- Consumer-editable files should use `create_if_missing: true` in `scripts/sync-targets.yml`.
- Files listed in `scripts/sync-targets.yml` are upstream-owned; consumer edits will be overwritten unless the target is skipped.

## Review Workflow

See [`.agents/REVIEW_WORKFLOW.md`](.agents/REVIEW_WORKFLOW.md) for the multi-lane adversarial review workflow.

## Cross-References

- [README.md](README.md) — install and consumer wiring.
- [scripts/sync-targets.yml](scripts/sync-targets.yml) — canonical sync surface.
- [CONTRIBUTING.md](CONTRIBUTING.md) — contribution workflow.
