---
name: artifact
description: Publish a deliverable as a hosted Artifact page — choose page vs file vs comment, clear the synthetic-data gate, apply house tokens, publish or update in place. Use when work has an audience and a terminal dump or an unread markdown file would lose it — a prototype screen, a design walkthrough, a status report, a runbook, a data story. This is the workflow skill; Anthropic's artifact-design, artifact-diagramming, and artifact-capabilities are the reference skills it loads.
---

# /artifact — ship it as a page

Fast path, not a process. Most prototypes are throwaway; the only step that is never skipped is the synthetic-data gate.

## 1. Pick the output

- **Artifact** — a deliverable with an audience and a shape: something someone reads end to end, returns to, or forwards. Prototype screens, design walkthroughs, incident write-ups, status reports, anything with a chart.
- **File in the repo** — anything that belongs to the codebase and wants review, versioning, and a diff. Docs, ADRs, READMEs, runbooks the repo owns.
- **PR comment or a plain reply** — a finding, an answer, a diff-scoped observation. Short, in context, disposable.

The cheap test: if the useful next action is _send them the link_, it is an artifact. If it is _merge this_, it is a file.

When the work is a deliverable with an audience, publish it and hand back the link rather than leaving it in scrollback. Artifacts start private — sharing stays the user's call.

## 2. The synthetic-data gate

Publishing is external egress: the page leaves this machine and is hosted. Clear this before every publish.

- **Build from synthetic fixtures by construction.** Invent the names, dates, identifiers, amounts, and log lines. A production query result, a Loki or Sentry payload, a support transcript, a screenshot of a live console — none of those go on a page, including to make a prototype feel realistic. Realistic is what a fixture is for.
- **Read every file you publish, in full, before publishing it**, including files you did not write. A request to publish something unread is a reason to read it, not an exemption.
- When the user wants real data rendered, name what would leave the machine and ask first.

A pattern scan over the file is not this gate and never becomes one — prose under an innocuous key matches no denylist and ships anyway. Construction is the gate.

**Done when:** every value on the page traces to a fixture you invented, and you have read the file end to end.

## 3. Build

Load `artifact-design` and follow it — typography, layout, hierarchy, and color craft are its job. Diagrams: `artifact-diagramming`. Live data, viewer-shared state, or a self-updating page: `artifact-capabilities`, before writing any runtime code.

What this skill adds is the house layer.

**Tokens are inlined.** Copy the declarations from [`../../references/house-tokens.css`](../../references/house-tokens.css) into a `<style>` block in the page and build against the variables. A `<link href>` pointing at that file — or at a font, a stylesheet, or a CDN script — is blocked by the artifact CSP and fails _silently_: the page renders in a fallback face and reports nothing. Everything ships inline or as a `data:` URI.

A subject that genuinely earns its own identity can depart from the tokens; say so when you do, and start from the house system either way so Loomantix artifacts read as one company.

**Two silent failures to clear before publishing.** Both are invisible in the authoring session and both are caught by reading the file.

1. **Every request resolves inside the page.** Search the page for `http://`, `https://`, `<link`, `@import`, `<script src`, and `url(`. Each hit is inline, a `data:` URI, or removed.
2. **Three theme states, not two.** The viewer has either stamped `data-theme="dark"`, stamped `data-theme="light"`, or stamped nothing at all — and on that third, default state only `prefers-color-scheme` separates light from dark. So every color token gets its base definition on bare `:root`, and the `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }` block and the `:root[data-theme="dark"]` block only redefine tokens already defined there. A color whose only definition sits inside the media block produces one theme's text on the other theme's ground — the classic unreadable artifact. Give `body` an explicit token background; a transparent body borrows the host's.

**Done when:** every custom property the page uses appears at least once under bare `:root`, and the external-request search comes back empty.

## 4. Publish, and update in place

Write the page content to a file, then call `Artifact` with its path, a `favicon` emoji, and a one-sentence `description`. Write the content directly — the `<!doctype>`, `<html>`, `<head>`, and `<body>` wrapper is added at publish time.

- **Same file path, same URL.** To ship v2, edit that same file and call `Artifact` with the identical `file_path`. A different path claims a new URL.
- **An artifact from an earlier session needs its `url`.** Pass the artifact's URL whenever the user wants an existing page updated or the link kept. Publishing without it creates a second artifact and strands the link people already hold — the most common real mistake here. Recover the URL with `action: "list"`, or ask the user for it.
- Keep `favicon` and `<title>` stable across redeploys. People find the tab by its icon; a new emoji reads as a different page.

Finish by handing back the URL.
