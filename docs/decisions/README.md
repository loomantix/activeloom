# Decision records

Decisions that more than one prompt root has to agree on. Most are deliberate
divergences between the Claude review chain (this repo) and its sibling engine
trees (`codex-platform`, `gemini-platform`): each of those names the divergent
files, states the behavioural difference concretely, and says why it stands. A
few fix a shared vocabulary instead.

[`parity-allowlist.yml`](parity-allowlist.yml) is where those records are
cashed in. `scripts/lint-prompt-parity.py` diffs every shared unrendered skill
across the prompt roots with the harness vocabulary normalized away, and fails
on any divergence that is neither `recorded` against a file below nor `held`
against a tracking issue and a residual ceiling. A divergence with no record is
presumed accidental.

| #    | Record                                                                                                                                                                                           |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0001 | [Pre-review session gate postures](0001-session-gate-postures.md) — hard gate in Claude and Gemini, advisory in Codex                                                                            |
| 0002 | [Reviewer precision postures](0002-reviewer-precision-postures.md) — report-everything-scored (Claude) vs high-precision (Codex, Gemini)                                                         |
| 0003 | [refactorpass architecture](0003-refactorpass-architecture.md) — one read-only cleanup worker (Claude), focused direct pass (Codex), inline Cleanup Matrix (Gemini)                              |
| 0004 | [reviewit as two documents](0004-reviewit-two-documents.md) — tier-authority orchestrator (Claude) vs resumable thin orchestrator (Codex, Gemini)                                                |
| 0005 | [copilot-review Fix Bias](0005-copilot-review-fix-bias.md) — Codex and Gemini only                                                                                                               |
| 0006 | [Review chains never converge](0006-review-chains-never-converge.md) — the standing record the others point at                                                                                   |
| 0007 | [agent-loop is per-harness](0007-agent-loop-per-harness-launch.md) — three launch models, three supervision models                                                                               |
| 0008 | [Proportionate Codex review](0008-proportionate-review.md) — lens selection and delegation chosen from risk, not from the skill name                                                             |
| 0009 | [Repository assurance facts](0009-repository-assurance-facts.md) — seven orthogonal repository fact domains, not a scalar assurance tier                                                         |
| 0010 | [Assurance control matrix](0010-assurance-control-matrix.md) — five independent control domains, trigger composition, and exact-head human review acceptance                                     |
| 0011 | [Assurance setup interaction](0011-assurance-setup-interaction.md) — context-first one-accept setup; the agent recommends facts and the resolver alone computes controls                         |
| 0012 | [Assurance ledger compatibility](0012-assurance-ledger-compatibility.md) — two new marker families, versioned finish and telemetry records, and an expand-then-use rollout                       |
| 0013 | [Invoking a review is not an escalation](0013-review-invocation-is-not-escalation.md) — a review request is not trigger 6, the tier order runs as returned, and the classifier owns human glance |
| 0014 | [Dependency review tier and hosted review boundaries](0014-dependency-review-tier-and-reviewit-boundaries.md) — dependency updates are Lean, and reviewit is never implicit or run on bot PRs    |

Most records here are parity divergences, and those are the ones
`parity-allowlist.yml` cites. A record may also fix a vocabulary that several
skills and tools are written against, in which case it is recorded here for the
same reason — one written source, so the harnesses cannot drift apart on it —
and simply never appears in the allowlist.

New records take the next number, use the same shape (Status / Date, the
decision, why it stands), and stay a few paragraphs — a record defining a
shared vocabulary is longer, because the vocabulary is the deliverable.
