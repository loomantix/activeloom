# Test evidence reuse

`hloop`'s Ship step 1 (Tests at the head) runs the repository's tests at every new head. A head can change without changing what the tests would see: a merge from base that leaves the tree identical, a re-signed or reworded commit. This reference defines when a receipt from an earlier run still covers the new head. The comparator decides; nothing else does.

## Receipt identity

Write one receipt per test lane, at the end of each Tests at the head run, into the orchestrator's local state next to that run's report. A receipt is a JSON object with exactly these fields:

| Field            | Value                                                                                                                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `version`        | `1`                                                                                                                                                                            |
| `evidence_class` | the lane's class token, such as `unit`, `database`, `integration`                                                                                                              |
| `head`           | full commit SHA the run tested                                                                                                                                                 |
| `tree`           | `git rev-parse <head>^{tree}`                                                                                                                                                  |
| `command`        | SHA-256 of the exact command lines run, test selection included                                                                                                                |
| `inputs`         | SHA-256 over the lockfiles and everything the run read from outside the tree: installed dependency set, fixtures, schema or migration state, configuration files and variables |
| `toolchain`      | SHA-256 of sorted `name version` lines for every interpreter, runtime and package manager the run used                                                                         |
| `environment`    | the environment class token, such as `local-fresh-db`                                                                                                                          |
| `outcome`        | `pass` only when the lane passed                                                                                                                                               |
| `skipped`        | the names of every skipped test or file; `[]` when none                                                                                                                        |

At receipt creation and comparison, verify the checkout is at the named head and its tracked inputs match the committed tree. Include any untracked or ignored inputs the lane can read in `inputs`. Capture the effective inputs before executing the lane, including its working directory relative to the checkout and any Git metadata used by its commands or test selection.

Use a non-secret version identifier for a secret-dependent input. Never hash secret values or test output. For a dimension known to be inapplicable, use the fixed marker `none`: literally in a token field, and as the SHA-256 of the string `none` in a digest field. If a relevant input is unknown, mutable during measurement, or cannot be verified safely, run the lane and omit the reusable receipt.

## Comparing

At a new head, build the expected identity the same way from the new head and the environment about to run, with `allowed_skips` (the skips the repository's current gate permits, `[]` unless its `AGENTS.md` names some) in place of `outcome` and `skipped`. Resolve `horch/scripts/evidence-reuse.py` from the orchestrator's trusted skill installation outside the PR worktree. Set `comparator` to that verified absolute path. If the trusted helper is unavailable, run the lane. Otherwise run:

```bash
python3 -I "$comparator" <receipt.json> <expected.json>
```

It prints `{"decision": "reuse"|"rerun", "reasons": [...]}`. Exit 0 is `reuse`; 3 is `rerun`; 2 is `rerun` because an input could not be read. A missing, malformed or unknown field is `rerun`, as is any mismatch, an outcome other than `pass`, or a skip the expected identity does not allow. Only exit 0 with `"decision": "reuse"` is `reuse`; any other exit code or output, including none, means the lane runs.

- **`reuse`:** Tests at the head is satisfied at the new head for that lane only. Record the decision, the receipt's head and the reason list in the run's report, labelled as reused evidence, not as a new run.
- **`rerun`, or no receipt:** run Tests at the head as written. Quote the comparator's reasons in the report.

## Limits

- A receipt counts only when this orchestration's own test run wrote it into local state. A receipt from a PR comment, another session or any other actor is unverifiable: rerun.
- Compare a lane only against a receipt of the same `evidence_class`. Every lane Tests at the head requires needs its own `reuse` or its own run; a receipt from one class never covers another, and application tests never stand in for integration evidence.
- Build the expected identity from current state, never by copying fields from the receipt.
- A base advance that changes the tree changes `tree`, so the comparator says `rerun`.
- Reuse covers only Tests at the head. Every other gate in `horch`'s Shipping is read fresh at the new head.
