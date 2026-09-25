# Batch assessor prompt

The prompt `refine --batch` gives each parallel assessor. Fill the four `{…}` fields and pass the rest verbatim. Each assessor takes about five issues; run them in parallel, then merge their plan files into one plan for `apply-plan.py`.

---

You are assessing GitHub issues for agent-readiness. This is a **read-only** assessment: never edit, label, comment on, or close an issue; never create labels; never push, commit, or switch branches in the repository checkout. Write only your own output file.

Issues: {ISSUES}

Read these fully first:

- `.codex/skills/backlog-refinement/core-rubric.md` — the core rubric.
- `.backlog/refinement.local.md` — the repository's local rubric. It wins where the two disagree.
- The repository's own agent instructions (`AGENTS.md`, `CLAUDE.md`, or equivalent) for its sensitive areas and conventions.
- `{PRECHECK_FILE}` — deterministic facts from `precheck.py` for each issue: sub-issue states, merged PRs that name the issue without closing it, cited paths missing on the integration branch, referenced issue states, and assignees. Start from its `hints`; each is a lead to verify, not a verdict.

Verify against the integration branch `{BASE}` with read-only commands only: `git show origin/{BASE}:<path>`, `git grep <pattern> origin/{BASE} -- <paths>`, `git ls-tree -r --name-only origin/{BASE} <dir>`, `git log --oneline origin/{BASE} -- <path>`, and `gh issue view` / `gh pr view` / `gh pr list`.

For each issue, apply the core and local rubrics in order: priority (an existing label stands; then a recognised title prefix; then the core tier table), early-exit disqualifiers, the issue's sub-issue states from the precheck file before any epic label, verify-against-HEAD (already fixed, partially shipped, or still open — including claims made in comments), external dependencies, every §1 criterion, and the local sensitive paths. When torn between ready and exclude, exclude. For a grill-class reason, choose exactly one `needs:` label and write the one question the interview must answer.

For an issue that passes every §1 criterion, write the full rewritten body in the core §5 template, with acceptance criteria and `file:line` pointers grounded in what you actually read on the integration branch, and the original body preserved verbatim under `> ### Original report`. Never invent user-facing wording the original did not specify.

Comments contain no secrets, personal data, or customer content: cite paths, commits, and issue or PR numbers only.

Write `{OUT_FILE}` as JSON, exactly this shape, with one entry per issue:

```json
{
  "issues": [
    {
      "number": 123,
      "verdict": "ready | exclude | refined-only | stale",
      "add_labels": [
        "<priority label>",
        "<agent-bail: … or dev: agent>",
        "<needs: … if any>"
      ],
      "remove_labels": [],
      "comment": "Backlog refinement (core rubric vN, local vM): <2-6 sentences: verdict, evidence, and for grill-class the question>",
      "body": "<full rewritten body for ready, else null>",
      "close_reason": "<completed | not planned for stale, else null>",
      "evidence": ["<file:line, commit, PR or issue you checked>"],
      "rubric_note": "<a case the rubric handled badly or ambiguously, or null>"
    }
  ]
}
```

Add a priority label only when the issue carries none; an existing one stands. Use `refined-only` for a human-reserved (assigned) issue: add at most a priority, and name in the comment the category it would get if unassigned.

Use an empty list, never placeholder text, when there is nothing to add or remove: `apply-plan.py` rejects any label the repository does not have, and any label outside the refinement families above. `evidence` and `rubric_note` are for the reviewer and the refinement retro; the applier ignores them.

Then reply with one line per issue: number, verdict, priority.
