# Backlog refinement — learnings

> Repository-owned, append-only RCA log. Every `agent-loop` bail or non-code PR failure produces an entry here through `backlog-refinement rca`. Each entry names the edit it produced: a change to `refinement.local.md`, an upstream candidate for the core rubric, or an `agent-loop-instructions.md` line. **Read it before a `refine --all` pass.**
>
> A common first finding across repositories: a stale backlog is the top cause of wasted iterations — issues whose described work already shipped. If your own entries show the same, treat verify-against-HEAD as non-negotiable before tagging.

---

## Meta-lessons

_Promote a pattern here only after this repository's entries show it recurring._

---

## Entries

_Newest first. One entry per distinct lesson._

```markdown
### <date> — #<issue> — <short title> [bucket A|B | <agent-bail category>]

- **Outcome:** PREVENTABLE | INHERENT
- **What could we have done differently:** <the answer, or "nothing — inherent">
- **Rubric/loop change:** <the edit, and whether it is local or an upstream candidate>
- **Evidence:** <commit / file:line / comment link>
```
