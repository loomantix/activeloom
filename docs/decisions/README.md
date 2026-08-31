# Decision records

Records of deliberate divergence between the Claude review chain (this repo)
and its sibling engine trees (`codex-platform`, `gemini-platform`). Each record
names the divergent files, states the behavioural difference concretely, and
says why it stands. A cross-tree parity lint allowlists deliberate divergences
by citing a record number; a divergence with no record is presumed accidental.

| #    | Record                                                                                                                                            |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0001 | [Pre-review session gate postures](0001-session-gate-postures.md) — hard gate in Claude and Gemini, advisory in Codex                             |
| 0002 | [Reviewer precision postures](0002-reviewer-precision-postures.md) — report-everything-scored (Claude) vs high-precision (Codex, Gemini)          |
| 0003 | [refactorpass architecture](0003-refactorpass-architecture.md) — delegation to `/simplify` (Claude) vs inline Cleanup Matrix (Codex, Gemini)      |
| 0004 | [reviewit as two documents](0004-reviewit-two-documents.md) — tier-authority orchestrator (Claude) vs resumable thin orchestrator (Codex, Gemini) |
| 0005 | [copilot-review Fix Bias](0005-copilot-review-fix-bias.md) — Codex and Gemini only                                                                |
| 0006 | [Review chains never converge](0006-review-chains-never-converge.md) — the standing record the others point at                                    |

New records take the next number, use the same shape (Status / Date, divergent
files, the behavioural difference, why it stands), and stay a few paragraphs.
