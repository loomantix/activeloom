---
name: docs-pass
description: Clarity and usability pass over a prose document — a how-to, runbook, onboarding guide, or README. Reports improvements rather than applying them, never deletes existing content, and never introduces an install command, version, path, or credential step it has not verified on this machine. Use when asked to clean up, clarify, tighten, or improve the readability of a document.
---

# docs-pass

A clarity pass over prose. It reliably improves readability and just as reliably
removes load-bearing content and invents plausible specifics, so this skill
separates the two: the readability work is proposed in full, and the two
destructive classes are reported as findings for the owner to decide.

## Mode

Run report-only. A prompt instruction not to edit is not enforcement — start the
session with `--mode plan`, which is. Apply edits only in a second pass the
owner asked for, after the report.

## Phase 1 — Read

Read the whole document before proposing anything. Identify what it is for and
who follows it. A how-to followed by someone with nothing installed has
different obligations from a reference.

## Phase 2 — Propose readability work

These are the improvements worth making, and the reason to run the pass at all:

- exact interactive prompt sequences, including prompts that display nothing
  while the user types;
- expected command output, so a reader can tell success from failure;
- first-time gotchas the writer has stopped noticing;
- click paths for GUI steps;
- end-to-end verification at the end of a procedure;
- ordering, headings, and sentence-level tightening.

Propose these as concrete replacement text.

## Phase 3 — Report, never remove

**Never delete existing content.** Rationale reads as verbosity and is not:
warnings, "why this matters" paragraphs, alternatives, standing rules, and
traps usually record an incident that the document exists to prevent.

When content looks redundant, report it as a deletion candidate with the text
quoted, and let the owner decide. The same applies to content being _replaced_:
a rewrite that drops a clause is a deletion.

## Phase 4 — Verify every new specific

**Never state a specific you have not verified on this machine.** Each of these
must be checked before it appears in proposed text, or the existing wording
stands unchanged:

- install commands and the installer actually in use;
- version numbers, including any pinned in an installer URL;
- expected-version comments against what is installed;
- file paths and directory layouts;
- credential sources, and who holds them.

A credential instruction naming a shared vault entry or an onboarder is a claim
about a credential that exists and may be handed over. Verify it exists, and
never propose text that implies account sharing.

Report anything you could not verify as an open question rather than filling it
with a plausible default.

## Phase 5 — Surface policy changes separately

A rewrite that changes what someone is permitted or required to do is a policy
change, not clarity — gates, approvals, waiting periods, who decides, and what
must be recorded. Report each one under its own heading with the before and
after text. It may well be the right change; it is the owner's to make.

## Output

Report, in this order:

1. **Readability proposals** — proposed text, per section.
2. **Deletion candidates** — quoted text, why it looks redundant, what it would
   cost to lose.
3. **Unverified specifics** — each claim, how it was checked, and the result.
4. **Policy changes** — before and after, flagged as policy.

If the owner then authorizes edits, apply them and review `git diff` filtered to
removed lines specifically. Reading the new text on its own will not show a
deletion; it reads fine without it.
