---
name: issues
description: GitHub issue workflow — work an issue end to end (claim, worktree, implement, draft PR), ready queue with dependency resolution, claim, close, link
---

# issues

Thin workflow over `gh issue` with a smart **ready** query that parses `Blocked by #N` / `Depends on #N` from issue bodies to compute dependency-free work. (`Blocks #N` is written as the reciprocal side by `issues link`, but isn't parsed as a blocker itself — the authoritative direction is `Blocked by`.)

**Arguments**: `$ARGUMENTS`

Dispatch on the first word of `$ARGUMENTS`. A bare issue number (`issues 123`) means `start 123`. If `$ARGUMENTS` is empty, default to `ready`.

---

## Permission hygiene

**Never use heredocs in `gh` commands** — the auto-approval regex can't match multiline commands, which causes permission prompts. Always write multiline content to a temp file first:

```bash
cat > /tmp/issue-body.md << 'BODY'
line 1
line 2
BODY
gh issue edit <n> --body-file /tmp/issue-body.md
```

Apply to: `gh issue create`, `gh issue edit`, `gh issue comment`.

---

## ready

Show open issues with no open blockers, sorted by priority.

```bash
./.codex/skills/issues/scripts/ready.py
```

Flags (all optional):

- `--mine` — only issues assigned to me
- `--unassigned` — only unassigned issues
- `--agent` — only issues labeled `dev: agent`
- `--priority critical|high|medium|low`
- `--area <name>` (matches `area: <name>` label — e.g., `backend`, `frontend`, `mobile`, `packages`)
- `--limit N` (default 20)
- `--json` — machine-readable

Exclusion rules:

- Label `status: blocked`
- Label `status: on-staging` — fix merged to a staging/integration branch, awaiting release/promotion (done, pending; an opt-in convention — no-op in repos that don't apply it)
- Any `agent-bail:*` label — explicitly excluded by backlog refinement or a prior loop run, even if a stale `dev: agent` label remains
- Body contains `Blocked by #N` or `Depends on #N` where #N is still open
- Targeted by a closing reference from an **open** PR, or from a PR **merged in the last 30 days** (via `closingIssuesReferences`, with a closing-keyword body fallback) — keeps issues already fixed as a PR side-item or by an in-review PR out of the queue, including done-on-integration issues that a non-default-branch merge never auto-closed

The `--agent` / `--priority` / `--area` flags work via standard label conventions (`dev: agent`, `priority: <level>`, `area: <name>`). Repos that don't use those labels will simply get an empty result for those filters — the script doesn't enforce a label scheme, it just queries one when asked.

---

## show \<n\>

```bash
gh issue view <n>
```

Surface dependency refs explicitly (useful for triage):

```bash
gh issue view <n> --json body --jq '.body' | grep -iE '^[[:space:]]*[-*]?[[:space:]]*(blocked by|blocks|depends on)[:\s]+#[0-9]+' || echo "(no dependency refs)"
```

---

## claim \<n\>

```bash
gh issue edit <n> --add-assignee @me
printf 'Claiming this.\n' > /tmp/issue-comment.md
gh issue comment <n> --body-file /tmp/issue-comment.md
```

---

## start \<n\> \[--setup-only\]

Take the issue from trigger to an open draft PR in one run: claim, isolate, implement, validate, publish. `--setup-only` stops after step 2.

Stop and report — rather than guessing — when:

- the issue is closed, assigned to someone else, labeled `status: blocked`, or lists an open `Blocked by` / `Depends on` ref;
- an open PR already closes it (`gh pr list --search "<n> in:body" --state open`);
- the issue fails the scope check in step 1.

### 1. Read, check scope, claim

```bash
gh issue view <n> --comments
```

Read the whole body and every comment. Required fixes, test invariants, "must preserve" and "out of scope" lists in the issue are the acceptance criteria for this run.

**Scope check.** The issue is ready to implement when all of these hold:

- **Outcome:** it says what should be true when done, not only what is wrong.
- **Done is checkable:** a reviewer could tell from the issue whether a PR finishes it — stated invariants, a reproduction that should stop reproducing, or concrete acceptance criteria.
- **Bounded:** it is one change that fits one reviewable PR, and names what is out of scope where the boundary is not obvious.
- **Direction chosen:** where several designs would satisfy it, the issue (or AGENTS.md and the code) picks one. At most one small open question remains.

Read the code the issue points at before judging; a short issue can still be fully scoped by the code it names. If one item fails narrowly, ask that single question and continue once answered. If more than one fails, or the missing piece is the direction itself, do not claim or branch: report which items fail, quote the gap, and recommend running `grill` on issue #<n>, recording the settled scope in the issue, then re-running `issues start <n>`.

Once the check passes, claim it:

```bash
gh issue edit <n> --add-assignee @me
```

### 2. Isolate in a worktree

Never `git checkout -b` in the primary checkout: other sessions may share it. Base the branch on the integration branch named in AGENTS.md if it names one (PRs target it), else the remote default:

```bash
base=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')
base="${base:-main}"   # override with the integration branch AGENTS.md names
slug=$(gh issue view <n> --json title --jq '.title' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-//' | cut -c1-40 | sed 's/-$//')
git fetch origin "$base"
git worktree add "../$(basename "$PWD")-issue-<n>" -b "fix/issue-<n>-$slug" "origin/$base"
```

Use the repo's own worktree location or branch-prefix convention instead when AGENTS.md sets one (`feat/` for a feature issue). Do all remaining work inside the worktree. Done when `git rev-parse --show-toplevel` prints the worktree path.

### 3. Implement

Make the change the issue asks for, in the layer it names, and add tests that encode each stated invariant. A regression test must fail against the base branch: run it once against the original file before relying on it. Track multi-part issues with update_plan.

Most lookups here are two greps and a read, and belong inline. Reach for delegation only where this session supports it and the lookup is both genuinely independent and too large for a handful of tool calls — a sweep across many files or repos. State a word ceiling on what it returns.

Done when every required fix and test invariant in the issue maps to a change and a test.

### 4. Validate

Run the typecheck, lint, and test commands AGENTS.md prescribes for the touched packages — the gating configuration, not a filtered subset. If a failure is yours, fix it; if it predates the branch, confirm that against the base and say so in the PR. Done when every command exits zero or each remaining failure is shown to exist on the base.

### 5. Commit, push, open a draft PR

```bash
git add <changed paths>
git commit -m "<type>(<scope>): <summary>" -m "Closes #<n>"
git push -u origin HEAD
gh pr create --draft --base "$base" --title "<type>(<scope>): <summary>" --body-file /tmp/pr-body.md
```

The body (write it to a temp file first) states what changed, any behavior change a reviewer should know about, the validation commands with their results and the commit SHA, and `Closes #<n>`. Keep it under ~250 words.

Done when `gh pr view --json url,isDraft,baseRefName` shows a draft against `$base`. Report the PR URL, the worktree path, and anything left undone. Review runs afterwards in a fresh session, per .codex/REVIEW_WORKFLOW.md; do not start it from this one.

---

## close \<n\> \[msg\]

With a comment:

```bash
printf '<msg>\n' > /tmp/issue-close.md
gh issue close <n> --comment "$(cat /tmp/issue-close.md)"
```

Without:

```bash
gh issue close <n>
```

**Confirm with the user before closing** if the issue isn't assigned to them or the close reason isn't obvious from the conversation.

---

## link \<n\> blocks|blocked-by \<m\>

Adds dependency refs to **both** issues so `ready` sees them regardless of which side you query.

```bash
./.codex/skills/issues/scripts/link.py <n> blocks <m>
./.codex/skills/issues/scripts/link.py <n> blocked-by <m>
```

- `link A blocks B` → writes `Blocks #B` to A, `Blocked by #A` to B
- `link A blocked-by B` → writes `Blocked by #B` to A, `Blocks #A` to B

Refs land under a `## Dependencies` section in each body. If a matching ref already exists, the script no-ops that side.

---

## search \<query\>

Forward to `gh issue list --search`:

```bash
gh issue list --search "<query>" --limit 20
```

Useful query fragments: `label:"dev: agent"`, `is:open no:assignee`, `in:title pipeline`, `author:@me`.

---

## Dependency parsing rules

`ready` recognizes these patterns (case-insensitive, on their own line, optionally bulleted):

- `Blocked by #N`
- `Depends on #N`

To mark a dependency, prefer `issues link` over manual edits — it keeps both issues consistent and under a parseable `## Dependencies` section. The label `status: blocked` is also honored (excludes from `ready` regardless of body content).

---

## Hard rules

- Never use heredocs in `gh` commands — always temp files.
- Never close someone else's issue without explicit user confirmation.
- Keep dependency refs under the `## Dependencies` section so they stay parseable and don't conflict with prose.
- If `ready` returns nothing, don't invent work — report the empty queue and stop.

---

## Source of truth

This skill is generated upstream from `prompts/skills/issues/` into `.codex/skills/issues/`, then synced to consumer repos. Edit the upstream source; consumer and generated copies are overwritten.
