# Review Workflow

This file is synced from `claude-platform` into consumer repos. Consumer edits
will be overwritten on the next sync.

## PR-First Rule

Open a draft pull request before any local cleanup or adversarial review. Local
Claude and Codex passes use GitHub review threads as durable shared context:
post each verified finding inline before editing, push the correction, reply
with the fix and validation, then resolve the thread. Every pass reads resolved
as well as unresolved threads before reviewing the current head.

Load [the local review ledger](references/local-review-ledger.md) before running
`refactorpass`, `critique`, `deepcritique`, `codex-review`, or local review hooks.

## Review Tier

Resolve the tier **before the first reviewer runs**, on every path. An
unresolved tier is not a neutral state — it is how the expensive path becomes
the default. **Lean is the default; Deep is the exception you justify.**

State the resolved tier and the trigger that selected it — or `no trigger` — in
the pass output, and post the ledger's `local-review-tier:v1` marker once per
PR. Later rounds read the marker instead of reclassifying; a tier re-derived
each round drifts back to Deep.

### What sets the tier

Tier is set by what a missed defect reaches, not by how hard the change is to
review. Difficulty is the wrong input: every subtle diff feels like it deserves
more scrutiny, and that feeling is what pulls a whole repo onto the deep path.

Resolve the changed-file list once with
`git diff --name-only <base-sha>..<head-sha>`, then walk the triggers below.
**Any one selects Deep; no trigger means Lean.**

1. **Sensitive path** — authentication, authorization, cryptography, secret or
   credential handling, PHI/PII, tenant or customer isolation. However small
   the edit.
2. **Irreversible in production data or a published artifact** — migration,
   backfill, a published package's API or version: anything a revert cannot
   undo.
3. **Fans out past this repo** — the synced `.claude/**` surface, the sync
   engine, a published package, a contract other repositories consume. One
   defect lands in every consumer.
4. **Non-obvious behaviour in deployed runtime code** — concurrency, retries or
   idempotency, cache invalidation, money or clinical calculation, state
   machines, partial-failure and rollback paths: correctness that is not
   readable from the diff.
5. **Recurring-incident area** — the touched paths produced a post-merge defect,
   revert, or hotfix in roughly the last 90 days
   (`git log --oneline --since=90.days -- <paths>`).
6. **Explicitly requested** — a deep review was asked for, or the change is a
   first of its kind the author cannot self-assess.

### What does not set the tier

Subtlety does not: a change can be hard to reason about and still be Lean. Nor
does diff size — a large mechanical refactor is Lean unless it also trips
trigger 4. Nor does topic adjacency: code _about_ security that does not itself
run on a sensitive path is not trigger 1, and a CI secret scanner is tooling
rather than auth.

**The dominant rule: when the worst outcome of a missed defect is a red CI run,
a broken build, or a broken developer workflow, the change is Lean.** CI
scripts, lint rules, build tooling, developer utilities, fixtures, and test
harnesses land here even when they are subtle and even when a defect in them
fails open. That class of defect is caught by the next person the tool touches
and fixed by editing the tool.

**Precedence: walk triggers 1–6 first. The dominant rule only resolves a change
that matched no trigger.** It is dominant over the difficulty instinct, not over
the trigger list. Tooling that also fans out past this repo — the sync engine, a
shared CI action, anything under `.claude/` — is trigger 3 and therefore Deep,
because its blast radius is not confined to the developer who runs it.

### Round budget and stopping rule

A round is one complete pass per available engine at the same head.

- **Lean — cap 2.** Round 1 is adversarial. Round 2 runs only if round 1 made a
  material fix, and runs in convergence mode.
- **Deep — cap 4.** Rounds 1–2 adversarial, rounds 3–4 convergence.

**Stop as soon as a complete round produces no material fix.** That is the
stopping rule for both tiers, and it is a rule rather than a budget to spend: a
Lean change that lands after one clean round has had enough review.

A Lean change that reaches round 3 has either been mis-tiered — escalate it
deliberately, below — or is not converging, which is a signal about the change
rather than a licence for another round. Say which, and stop.

### Escalate and de-escalate on evidence

Both moves require a confirmed finding. A suspicion, an unverified severity
label, or "this feels risky" is not evidence and does not move a tier.

**Lean → Deep.** Escalate when a confirmed finding shows the change reaches a
trigger the classification missed — a real authorization or isolation bypass, a
real data-shape change, a real break in a contract another repository consumes.
Name the finding and the trigger, update the tier marker, and adopt the Deep
budget. The round already run counts as Deep round 1; do not restart the count.
**The first round after an escalation is adversarial whatever its ordinal.** An
escalated change would otherwise inherit a convergence stance and receive the
Deep budget without one adversarial Deep pass — and escalation fires precisely
when a confirmed finding showed the change reaches further than classified.

**Deep → Lean.** De-escalate when Deep round 1 completes and _every_ lens owning
a recorded trigger returned no confirmed finding. Finish at Lean: the lean lens
set, one further round at most. Running the full matrix again over a
substantively unchanged diff audits the review rather than the change. Record
the de-escalation and the lenses that came back clean.

Trigger 6 — an explicitly requested deep review — is never de-escalated. The
request is the evidence, and no clean lens overrides it. For the rest, a trigger
de-escalates only through the lens that owns it:

| Trigger                          | Owning lens                                 |
| -------------------------------- | ------------------------------------------- |
| 1 sensitive path                 | `security-review`                           |
| 2 irreversible data or artifact  | `code-reviewer` (migration/compat pass)     |
| 3 fans out past this repo        | `code-reviewer` on the consumed contract    |
| 4 non-obvious deployed behaviour | `silent-failure-hunter` + `code-reviewer`   |
| 5 recurring-incident area        | `code-reviewer` scoped to the incident path |
| 6 explicitly requested           | not de-escalatable                          |

Tier selection narrows which lenses run and how many rounds are owed. It never
narrows what a lens may report, and it never relaxes the post-before-editing,
reply, or resolve contract.

## Local Convergence Path

Use this path when both local engines are available:

1. Make the change, validate it, create a clean commit, and open a draft PR.
2. Pin the exact base SHA for the round, resolve the tier, and give both to
   the reviewers. Do not start a reviewer with the tier unresolved.
3. Run `codex-review <pr-number>` as a fresh local Codex pass. Read the ledger
   and apply the comment/fix/reply/resolve contract to confirmed findings.
4. On the resulting head, run the Claude lane for the resolved tier in a fresh
   session, under the same ledger contract: `critique <pr-number>` at Lean,
   `deepcritique <pr-number>` at Deep.
5. Classify fixes by effect, not path or finding severity. `material` includes
   substantive correctness, security/privacy, data-safety, compatibility,
   deployment/sync, or review-integrity changes, including tests or workflows
   needed to prevent a false green. `minor` is low-risk non-behavioral cleanup
   or polish. Restart at Codex after a material fix; retain minor fixes.

   **The chain gets cheaper as it repeats.** Two rules make that happen, and both
   are enforced from the ledger rather than from session memory:
   - **The refactor pass runs once per engine per PR.** A second `/simplify` over
     an already-simplified diff returns naming and shape churn, which moves the
     head and re-stales the other engine's attestation for nothing that ships.
     Each engine's cleanup lane latches on a `local-review-refactor:v1` marker;
     a docs/config-only skip does not consume it.
   - **At Deep, rounds 1–2 are adversarial and round 3 and later are
     convergence rounds; at Lean the cap is 2 and round 2 is the convergence
     round.** The stance follows the tier's schedule above, not the ordinal
     alone.
     Once both engines have read the change cold twice, the remaining findings
     are mostly about the review's own artifacts. A convergence round runs only
     the lenses that can find a reason not to deploy, changes the PR only for a
     realistically reachable blocking defect, defers everything else, creates
     an issue only for an urgent high-impact follow-up, and ends the loop as soon
     as it finds no blocker. Lenses still report everything they find —
     the narrowing is a disposition rule applied by the orchestrator, never an
     instruction to a review agent to withhold by severity or confidence.

6. Converge after one complete Codex-then-Claude round has no material
   transition, every pass has a validated v3 result for its exact reviewed head,
   and every local-review thread has a disposition reply and is resolved. A
   minor A-to-B transition can complete the round without pretending the first
   engine reviewed B; its exact-head attestation remains historical evidence.
7. The wrapper, not review hooks, posts canonical pass/completion attestations
   after validating structured results and the GitHub ledger.
8. Stop at the tier's round cap — two at Lean, four at Deep — or earlier under
   the stopping rule above. Leave the PR draft and report non-convergence
   instead of continuing an unbounded cycle.

Do not add hosted reviewers to this path merely as another ritual. A later
hosted-review fix invalidates local convergence and requires a fresh local
round.

## Hosted Fallback Path

When a local Codex CLI is unavailable:

### Lean

1. Open a draft PR.
2. Run `refactorpass <pr-number>`, then `critique <pr-number>`.
3. Run `reviewit <pr-number>` for the bounded Gemini Flash and Copilot loop.

### Deep

1. Open a draft PR and run `deepcritique <pr-number>`.
2. Run `reviewit <pr-number> deep`; its final local `deepcritique` receives the
   same PR number and ledger. That tail `deepcritique` skips the refactor pass —
   step 1 already spent this engine's cleanup latch on the PR.

Which of the two runs is the tier decision above, resolved before the first
reviewer — not a per-invocation choice. `reviewit`'s iteration cap is the tier's
round cap: two at Lean, four at Deep.

## Review Principles

- Treat generated findings as hypotheses; verify against source before posting.
- **A pass that cannot name its tier and the trigger that selected it has not
  started correctly.** Tier is resolved before the first reviewer, not inferred
  from which skill someone happened to type.
- **No reviewer in this chain pre-filters by severity or confidence.** Not a
  `critique` sub-agent, not `codex-review`, not an inline `Agent(...)` prompt you
  write yourself. Each reports everything with a severity and confidence
  attached; the filtering happens one level up, where every lens is visible at
  once and each claim can be checked against the diff. A finding suppressed
  inside the reviewer is unrecoverable; a low-scored finding costs one line to
  dismiss. See [`MODEL_NOTES.md`](MODEL_NOTES.md) §1.
- The agent matrix is a ceiling, not a floor. Run only the lenses whose signals
  appear in the diff, and never add an agent to re-check another agent's work.
  See [`MODEL_NOTES.md`](MODEL_NOTES.md) §2–§3.
- Fix a confirmed finding only when likely user harm or a credible security
  exploit justifies the fix's churn and regression risk.
- **A round that only finds non-material test, fixture, comment, or docs polish
  is the signal to ship, not to keep going.** It means the product converged and
  the review has turned to auditing its own artifacts. A test or workflow fix
  needed to prevent a false green remains material and restarts at Codex under
  step 5. Defer non-material polish without growing the backlog.
- Create a tracking issue only for a concrete, high-impact follow-up that should
  be scheduled within roughly two weeks.
- A fix without a preceding inline finding, a finding without a reply, or a
  resolved thread without a visible disposition is a failed pass.
- Never copy sensitive source, credentials, private data, or model logs into PR
  metadata.
- Stop at the configured cap and preserve the draft PR on non-convergence.

## Cross-references

- [`MODEL_NOTES.md`](MODEL_NOTES.md) — prompt-authoring deltas for the current
  default model; read before editing any skill or agent.
- [`references/local-review-ledger.md`](references/local-review-ledger.md) — the
  PR-thread ledger contract, including the shared docs/config-only changeset
  classification every skill skips on.
- [`skills/refactorpass/SKILL.md`](skills/refactorpass/SKILL.md) ·
  [`skills/critique/SKILL.md`](skills/critique/SKILL.md) ·
  [`skills/deepcritique/SKILL.md`](skills/deepcritique/SKILL.md) ·
  [`skills/codex-review/SKILL.md`](skills/codex-review/SKILL.md) — the local
  convergence lanes.
- [`skills/reviewit/SKILL.md`](skills/reviewit/SKILL.md) — the hosted fallback,
  including the `tier=flash` cost rule.
- [`skills/review-accessibility/SKILL.md`](skills/review-accessibility/SKILL.md)
  — optional, human-triggered a11y pass; opens its own PR and is not part of
  either path above.
- `/pushit` and `/review-cycle` are retired; their stubs are gone, so old
  invocations resolve to nothing.
