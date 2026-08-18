# Security Policy

This repository ships Google Antigravity and Gemini CLI skills, role references, and a consumer sync engine. While skills do not store secrets directly, the sync engine and commit helper scripts execute inside CI environments with repository tokens. A vulnerability in this repository could affect downstream consumers that sync from it.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security concerns responsibly:

- For private security disclosures, use GitHub's private vulnerability reporting feature on this repository, or email security disclosures to the maintainers.
- Include reproduction steps or proof-of-concept, affected files/scripts/skills, and contact information.

## Scope

In scope:

- Vulnerabilities in `scripts/sync-engine.py` or `scripts/create-signed-commit.py` that could allow path traversal, arbitrary file write, token exfiltration, or supply-chain compromise of consumers.
- Vulnerabilities in `templates/sync-from-gemini-platform.yml` (the canonical consumer workflow template) that could leak secrets, escalate permissions, or bypass review gates.
- Skill instructions that could drive Antigravity or Gemini CLI into destructive actions (e.g. unintended `git push --force`, secret disclosure, unauthorized modifications beyond stated scope) when the skill is invoked under its documented contract.
- CI and build supply-chain vulnerabilities in this repository's own workflows.

Out of scope:

- Vulnerabilities in upstream runtime engines (Google Gemini CLI, Antigravity CLI, PyYAML, or external GitHub Actions).
- Misconfiguration of downstream consumer repositories. Consumers maintain their own operational security boundary.

## Disclosure Policy

We follow coordinated vulnerability disclosure:

- We acknowledge valid reports promptly.
- We work with researchers to validate, patch, and release fixes.
- Security advisories are published alongside releases.
