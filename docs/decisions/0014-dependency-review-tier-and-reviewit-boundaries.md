# 0014 — Dependency review tier and hosted review boundaries

- Status: accepted
- Date: 2026-09-25

## Context

Two misinterpretations led to unnecessary review overhead and unexpected reviewer
dispatches:

1. **Dependency version bumps escalated to Deep review.** When updating an external
   dependency version in package manifests and lockfiles (e.g. `@nestjs/jwt`,
   `class-validator`, `opossum`), review engines treated the package's functional
   domain (authentication, validation, circuit breaking) as tripping Trigger 1
   ("Sensitive path") or Trigger 4 ("Non-obvious runtime behaviour"). However, a
   manifest/lockfile-only diff contains no repository application logic or
   third-party source code to critique; multi-lane code reviewers inevitably report
   zero findings. Dependency risk belongs in upfront research, release notes,
   advisories, and running the consumer test suite—not in a multi-lane code review
   chain.
2. **Unexpected invocation of hosted reviewers.** When users asked to "run the review
   chain" or review a PR, review engines sometimes reached for hosted reviewers
   (`reviewit`, which dispatches the Gemini Code Review workflow and requests
   Copilot reviews via GraphQL). Rather than littering skill prompts with defensive
   negative instructions and bot author inspections, hosted review availability
   belongs at the setup layer (`review-setup` and `review-profile.py`).

## Decision

- **Dependency bumps do not select Deep.** Upgrading an external dependency in
  manifests or lockfiles (`package.json`, `pnpm-lock.yaml`, `Cargo.lock`, etc.)
  does not trip Trigger 1 or Trigger 4 based on the package's subject domain.
  Unless accompanied by material application code changes touching sensitive or
  non-obvious paths, dependency version updates are Lean.
- **Hosted review availability is configured at setup.** `review-setup` asks
  the user whether hosted review (`reviewit` — Gemini Flash + Copilot) is an
  option they want available. When a user declines or operates locally only,
  `reviewit.availability=unavailable` is recorded in `review-profile.json`.
- **Preflight enforcement via review-profile helper.** `reviewit`'s preflight
  runs `review-profile.py check --need reviewit`, which exits with code 1
  (`EXIT_REFUSED: marked unavailable: reviewit`) when unavailable, cleanly halting
  execution without custom prompt logic.
- **Ambiguity resolved by asking the user.** When both local and hosted review
  are available and user intent is ever ambiguous (such as "review this PR"),
  the agent asks the user to specify which type to use rather than guessing or
  picking remote review.

## Consequences

Dependency bumps stay on the Lean path with fast, high-signal verification via the
test suite rather than hollow multi-lane critique passes. Hosted review availability
is governed cleanly by user profile configuration and helper preflight checks,
keeping skill prompts simple and free of defensive bloat.
