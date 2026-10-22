---
name: red-team
description: Pre-merge red team of a reviewed PR — finds how the change fails once live, then sorts each finding into fix-before-merge, follow-up issue, or accepted risk for the user to decide.
disable-model-invocation: true
argument-hint: (optional) PR number
---

# /red-team — find how a reviewed change fails before it merges

Run this after the review chain has converged and before merge. The chain asks whether the diff is correct. This pass asks how the change fails once it is live: the inputs, callers, states, and operations the diff never mentions. It reports findings with a recommendation, the user decides which to fix now and which become follow-up issues, and only then does it act.

It is not a review-chain pass. It writes nothing to the review ledger: no finding threads, dispositions, attestations, or tier or run markers.

## Step 1: Pin the target

**Arguments:** an optional PR number. Without one, use the current branch's open PR; with no PR, use the branch diff against its merge base with the default branch.

1. Record the base and head SHAs. Every finding is evidence about that head.
2. If this session wrote the change, recommend a fresh session and continue only on an explicit override. A session that wrote the code reads it already holding the reasons it is right, which is the opposite of this pass's job.
3. Read the PR body, its linked issues, and every review thread, resolved and outdated included. A resolved thread is a claim about a fix, not a settled question: the fix is in scope.

Done when you can state the head SHA, what the change is meant to do, and what the review chain already found and fixed.

## Step 2: Hunt

Read the whole diff, then work outward to what it touches but does not show: callers, consumers of a changed contract, configuration in other environments, migrations, and deploy order. Scrutinize each angle whose signal is present:

| Angle            | Questions                                                                                                                         |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Inputs           | What do malformed, empty, oversized, duplicated, out-of-order, or adversarial inputs do?                                          |
| Trust boundaries | Who can reach this? Can a caller get access, data, or side effects it should not — injection, traversal, missing authorization?   |
| State and timing | What happens on retry, partial failure, concurrent calls, re-entry, or a timeout mid-write? Is it idempotent where it must be?    |
| Data             | Is any write irreversible? Do old and new versions read each other's data during a rolling deploy? Does a backfill finish safely? |
| Operations       | Can it be rolled back? Do defaults hold in every environment? Would anyone notice it failing — logs, metrics, alerts?             |
| Blast radius     | Which other callers, packages, or repositories depend on what changed, and do they still work?                                    |
| Prior fixes      | Does each review-chain fix hold, or did it move the defect somewhere the review did not look?                                     |
| Tests            | What would still pass if the behavior were wrong? Which test asserts the mock rather than the system?                             |

Name the angles you skipped, and why, in one line.

Each finding needs a concrete failure scenario — this input or state, therefore this wrong outcome — and a `file:line`. Where it is cheap and safe, demonstrate it: run the relevant test or a throwaway reproduction outside the repository, never committed. Report every finding you believe is real with your confidence in it; low confidence is information for Step 3, not a reason to leave a finding out.

Work inline by default. For a large change with genuinely independent areas, delegate at most three of them where this session supports delegation, each scoped to its files and the angles that apply, returning findings in the Step 3 shape in under 300 words. Delegate areas of the change, never a second look at another agent's findings.

Done when every applicable angle has been worked against every changed file and every outward dependency you found.

## Step 3: Triage and report

Give each finding a severity (`blocker`, `high`, `medium`, `low`), a confidence (`high`, `medium`, `low`), and one recommendation:

- **Fix before merge** — merging exposes users or data to it and it is hard to undo: data loss or corruption, a security exposure, a broken rollout or rollback, a broken contract for existing callers, or a test that passes while the change's own behavior is wrong. A finding whose fix is small and local also belongs here.
- **Follow-up issue** — real, but merging does not make it worse: hardening, a scaling limit well beyond current load, missing observability, or a fix large enough to need its own review. A pre-existing defect is a follow-up unless this change makes it reachable; label it pre-existing.
- **Accept** — it needs conditions the system rules out. Name the condition.

Report in this shape:

```
| # | Severity | Confidence | Finding | Location | Recommendation |
```

Then, per finding, two to four lines: the scenario, the evidence, and the suggested fix. Close with the counts per recommendation and the head SHA. Stay under 600 words unless the number of findings requires more. No findings is a valid result; say which angles you worked.

Then stop and ask the user to decide: accept the recommendations as they stand, or move findings by number. Act on nothing until they answer.

## Step 4: Act on the decisions

**Fix before merge.** Fix on the PR branch, one commit per finding or tightly coupled group, and run the validation the repository's AGENTS.md or CLAUDE.md names. Push normally. A push moves the head past the review chain's attestations; say so, and leave it to the user and the repository's merge policy whether the chain runs again.

**Follow-up issue.** Search the open issues for an existing one first and link it instead of filing a duplicate. Draft one issue per remaining finding: a title, the scenario, the evidence, the suggested fix, and a link to the PR and head SHA. Also draft one PR comment listing the follow-ups, so whoever merges sees them. Show every draft in a single preview and create them on one confirmation.

Issue bodies are public wherever the tracker is. Keep credentials, private data, and reproduction detail that would let someone exploit a security finding out of them. A security finding in a public repository follows the repository's `SECURITY.md` disclosure path instead of a public issue, and stays out of the PR comment too; that comment says at most that one finding was reported privately.

**Accept.** No action; it appears in the summary with its stated condition.

Done when every finding has an outcome: fixed with a commit SHA, filed or linked with an issue URL, accepted, or failed with the error. Report that list and the PR's new head SHA.

## Scope

- The only GitHub writes are the fix commits for findings the user chose and the issues and comment they confirmed in the preview.
- This skill does not merge, approve, mark a PR ready, or force-push.
