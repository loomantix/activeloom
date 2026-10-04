---
name: internal-docs
description: Find, author, validate and locally preview repository-owned internal documentation using the consumer's documentation command. Use for internal handbook, ADR, runbook or documentation artefact work.
---

# Internal docs

Read the repository's `AGENTS.md` and `internal-docs.config.json`.
The consumer owns its command array, setup guide, approved collections and
publication authority. Missing configuration or shell access means the workflow
is unavailable: explain the missing prerequisite and use the documented manual
equivalent. A skill installation grants no source or publisher access.

1. Run the configured command with `doctor --sources <local mapping JSON>`.
   Map canonical repository identities to explicit task checkouts; never infer
   private paths, credentials or a repository from a conversation. Use `find`
   before authoring, then `read --id <stable ID>` for the selected immutable source.
2. For an existing page, use `update --id <stable ID>` and edit its canonical
   source in that repository's dedicated worktree. Preserve its identity/route.
   For a new page, use `create` with an approved collection, source path, slug,
   title and template kind. Review the resulting manifest proposal separately;
   creation is a draft, not content-owner or audience approval. An unconfigured
   collection needs an explicit consumer configuration proposal first.
3. Keep owner, audience, content-review date, document revision and application
   evidence separate. Unknown metadata stays unresolved. Preserve the existing
   information architecture; renames/removals need redirects or retirement review.
4. Run `validate` and `prepare` with the explicit source mapping, then `preview`.
   Publication preparation reads only approved immutable revisions. For local
   uncommitted work use `prepare --draft`, then `preview --draft`; clearly label
   this output and never pass it to a publisher. Follow the consumer's build,
   link/anchor and browser checks. Failed preparation invalidates earlier output.
5. Run `status` and prepare the source/manifest PRs and their native review.
   Report exact revisions, validation, unresolved ownership, local preview and
   the next publisher step. Report a published URL only with verified deployment
   evidence; a proposed route, prepared build or historical release is not proof
   of current publication. Do not deploy as a side effect of an ordinary answer.

Markdown is the initial supported type. Static images/downloads require a
reviewed consumer type registry. Interactive HTML/JavaScript and MDX remain
unavailable until their isolation contract is implemented and reviewed. Retain
source importer guards; never copy browser cookies or request administrator
credentials to bypass missing publication/preview integration.

Use the harness's shell tool to run the configured command array with separate
arguments. For clients without native skill activation, read this file explicitly
and follow the same CLI/manual workflow. See the consumer setup guide for
installation, diagnostics and recorded runtime support; files alone do not prove
native harness discovery or a fleet rollout.
