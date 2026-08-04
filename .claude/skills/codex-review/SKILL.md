---
name: codex-review
description: Independent PR-first second opinion via the local Codex CLI. Codex finds issues read-only; Claude verifies them, posts confirmed findings inline before editing, then pushes, replies, and resolves. `verify` lets Codex run tests/build.
argument-hint: <pr-number> (optional "verify")
---

# /codex-review — independent Codex cross-review

You are getting an **independent opinion** on an open PR from the [Codex CLI](https://github.com/openai/codex), run locally. Codex is a different model family from the Claude review chain (`/grill`, `/deepgrill`). In the bounded local loop, Codex runs first in each round and reads the complete PR ledger cold; Claude `/deepgrill` follows on the resulting head.

Codex runs **read-only by default** — it can read the tree and reason, but cannot modify files, so it is a safe reviewer. This skill never lets Codex edit code. Findings come back to _you_; you verify each against the source and fix only the confirmed ones.

## When to use

- Before `/deepgrill` in each bounded local round, including after any material
  Claude fix restarts the loop.
- Standalone, when you want a fresh cold read of a PR.
- Skip on docs/config-only changesets — there is nothing for an adversarial reviewer to find.

## Phase 0: Pre-flight

1. **Codex must be installed and authenticated.** Check:

   ```bash
   command -v codex && codex --version
   ```

   If it is missing, stop and tell the user to install and authenticate the Codex CLI (`npm i -g @openai/codex`, then `codex login`) — this skill cannot proceed without it. Authentication is machine-level, so once it is set up any session can use it.

2. **Resolve the PR and durable context before starting Codex.**
   - `$ARGUMENTS` must contain a PR number and may contain `verify`.
   - Load [`../../references/local-review-ledger.md`](../../references/local-review-ledger.md).
   - Fetch the PR head without repurposing a primary checkout. Require the current linked worktree branch, its remote head, and the PR head to match.
   - Read every prior review thread, including resolved and outdated threads. Summarize their fingerprints, dispositions, fix SHAs, and reviewed-head attestations for the Codex prompt.
   - Read the PR base with `gh pr view <n> --json baseRefName`.
   - Compute the merge base and capture the authoritative tracked and untracked scope:

     ```bash
     BASE=origin/<base-branch>
     git fetch origin <base-branch> --quiet
     MB=$(git merge-base "$BASE" HEAD)
     git diff --stat "$MB"         # committed, staged, and unstaged tracked changes
     git status --short
     git ls-files --others --exclude-standard
     ```

   - If the changeset is docs/config-only per the ledger's changeset classification, post a scoped clean-pass attestation and exit.
   - Resolve the Codex engine's round number per the ledger: `$AGENT_LOOP_REVIEW_ROUND` when the runner set it, otherwise one past the count of `local-review-pass:v1` and `local-review-complete:v1` markers on the PR naming `engine=codex`. Rounds 1–2 are adversarial; round 3 and later are convergence rounds, and the prompt and dispositions change accordingly.

## Phase 1: Build the review prompt

Write a tight, scoped prompt. A vague "review this" wastes the run; name the files and the riskiest failure modes. Include:

- One line on what the repo is, **that it is our own code**, and — **if the repo is or may become public — an instruction to never print secrets, ARNs, account ids, or hostnames** in its output.
- 2–3 lines on what the change does.
- A concise summary of the complete local-review ledger, including resolved
  and outdated threads, so Codex does not rediscover disposed defects.
- The tracked diff to read (`git diff <MB>`) plus the untracked paths from `git ls-files --others --exclude-standard`, and an instruction to **read the actual source, not just the diff**.
- The 3–4 riskiest things about this specific change, phrased as **where to scrutinize hardest** — not as an attack. See the framing rules below.
- The output contract: **only high-confidence material findings** (correctness, security, data-loss); for each, `file:line`, severity, concrete issue, concrete fix; "no material findings" if clean; be terse.

### Convergence rounds (round 3 and later)

Codex has already read this change cold twice. Narrow the prompt's scrutiny list
to what could stop the deploy — correctness, data safety, security and privacy,
broken public contracts, rollout breakage — and drop the design, docs, and test
angles from it. Say plainly that the change is converging and the question is
whether anything blocks shipping, not whether it could be better.

Keep the terse-but-complete output contract as written. The narrowing belongs in
what Codex is pointed at and in how you disposition what comes back, not in an
instruction to withhold findings it already made.

### Optional repo context

Repo-specific knowledge — how to run the suite and the traps in doing so, which invariants are structural, which questions are already settled — lives outside this skill so the skill stays generic and portable. Before writing the prompt, check for a context file at `.claude/codex-review.local.md` in the current repo (or one the user names) and read it. If absent, build the prompt from this skill alone.

That file is also where a **reusable prompt scaffold** belongs, and the reason is worth stating: a prompt written ad hoc into a scratch directory gets opened as the template for the next round, and the round after that. Lines that were true when first written — a permission sentence, a suite's pass count, a "do not re-raise" list, a defect-class tally — ride along unexamined into runs where they are false. Keep the scaffold in the repo where it can be corrected once, and keep the parts that change every round (head SHA, the delta, the ledger summary) out of it.

### Framing: write it as internal QA, not as an attack

The reviewer is another vendor's model with its own safety classifiers. A security-focused review of code you own is entirely legitimate, but **offensive-security phrasing can get the run refused mid-pass** — and a refusal burns the whole multi-minute, rate-limited run and reads deceptively like a clean review. Frame every prompt so its legitimacy is obvious from the text alone:

- **Lead with ownership and purpose.** One line, always: _"This is our own repository. This is a routine pre-merge quality and security review of a change we wrote. Report findings only — make no changes."_ Ownership plus review-intent is what separates QA from targeting someone else's system, and the model cannot infer either one.
- **Never direct offensive verbs at the code.** Drop "attack these", "exploit this", "break it", "pwn", "hack", "bypass the auth", "red team this". Ask instead: _"Scrutinize these areas hardest"_, _"where is this most likely to be wrong?"_, _"under what inputs does this produce a wrong result, lose data, or grant access it shouldn't?"_ Same coverage, no trigger.
- **Ask for the defect and the fix, never for a weapon.** The deliverable is the failing input shape, the wrong outcome, and the concrete fix. Do not ask for a working exploit, a PoC payload, or reproduction steps against a live system — none of that is needed to act on a finding, and all of it invites a refusal.
- **Keep the target the diff, not a system or a person.** Don't name live hostnames, customers, or production endpoints as things to probe. "Review this auth middleware" is fine; "get past the login on <host>" is not, and it is not what you want anyway.
- **Don't paste credential-shaped strings** into the prompt, even as illustrative examples. Refer to them by variable name.
- **The rules cover the whole run**, not just the opening prompt: follow-up turns, `verify`-mode instructions, and any repo-level instruction file the reviewer auto-loads (e.g. `AGENTS.md`). In `verify` mode, "run the test suite and the build" is fine; "attack the running service" is not.

If a run does come back refused, treat it as a **failed run** and re-frame — see Phase 3.

## Phase 2: Run Codex (read-only, streaming)

Run the dedicated non-interactive reviewer. **Flags verified against `codex-cli 0.145.0`** — the CLI surface drifts, so check both `codex exec --help` and `codex exec review --help` after an upgrade. Use `codex exec review`, not the top-level `codex review`, because the `exec` form exposes model, output-file, and automation controls. There is no `--ask-for-approval` or `--full-auto` flag on this subcommand. The flags that matter, and four traps:

- `-c 'sandbox_mode="read-only"'` — `exec review` no longer exposes `--sandbox`; set the equivalent config explicitly so Codex cannot touch the tree. (Modes: `read-only` · `workspace-write` · `danger-full-access`.)
- `--skip-git-repo-check` — lets it run in a worktree / subdir without complaining.
- `-o <file>` (`--output-last-message`) — writes **only** Codex's final message (the findings) to its own file. Without it you have to dig the findings out of the bottom of a huge stream that also echoes the prompt, `AGENTS.md`, and every file Codex auto-read.
- **Trap 1 — never pipe through `tail`/`head`.** They buffer until the process exits, so a multi-minute run looks hung with zero output. Redirect straight to a file.
- **Trap 2 — contention.** Many parallel Codex runs (across sessions) rate-limit each other and slow down. Prefer one at a time.
- **Trap 3 — scope selectors conflict with custom prompts.** In 0.145.0, `--base`, `--commit`, and `--uncommitted` cannot be combined with `[PROMPT]`, despite the generated usage line showing both. The prompt already names the authoritative merge-base scope, so do not add a selector. Keep `</dev/null` so generic `exec` stdin-append behavior cannot block or contaminate the prompt.
- **Trap 4 — `pkill -f` matches the shell doing the killing.** A command such as `pkill -f "codex exec"` also matches the shell running it. Instead:
  - **Never put a kill and a relaunch in one bash invocation.** Kill, confirm, then start the new run separately.
  - Kill the exact PID printed by the launcher below. Use `pgrep -f "[c]odex exec review"` only to identify a lost PID.
  - **Check who else is running Codex first** (`pgrep -af codex`). A developer or another agent session may have a long Codex turn open on the same machine; a broad `-f codex` pattern takes theirs down with yours.

```bash
RUN_ID="$(date +%s)-$$"
CODEX_FINDINGS="/tmp/codex-findings-${RUN_ID}.md"
CODEX_LOG="/tmp/codex-full-${RUN_ID}.out"
CODEX_STATUS="/tmp/codex-status-${RUN_ID}.txt"
(
  status=0
  child_pid=""
  terminate_child() {
    [[ -z "$child_pid" ]] || kill -TERM "$child_pid" 2>/dev/null || true
  }
  trap terminate_child HUP INT TERM
  codex exec review \
    -c 'sandbox_mode="read-only"' \
    --skip-git-repo-check \
    -o "$CODEX_FINDINGS" "$REVIEW_PROMPT" \
    </dev/null >"$CODEX_LOG" 2>&1 &
  child_pid=$!
  wait "$child_pid" || status=$?
  trap - HUP INT TERM
  printf '%s\n' "$status" >"$CODEX_STATUS"
  exit "$status"
) &
CODEX_PID=$!
printf 'pid=%s\nfindings=%s\nlog=%s\nstatus=%s\n' \
  "$CODEX_PID" "$CODEX_FINDINGS" "$CODEX_LOG" "$CODEX_STATUS"
```

Run it in the background and retain the four printed values. Tail the log for liveness. The status file appears only after exit and contains Codex's exit code; use it as the completion check because 0.145.0 does not consistently print the old `tokens used` marker. Read the findings file only after a zero status. Codex may load its own review skills, so a broad prompt can still trigger a thorough, slow pass.

**Runtime defaults (why it is slow).** Codex commonly defaults to a frontier model at high reasoning effort. For a faster pass, add `-c model_reasoning_effort=medium` (or `-m <model>`), and for a small diff tell Codex to do one focused pass and read only the named files.

### `verify` mode (opt-in)

If `$ARGUMENTS` contains `verify`, the user wants Codex to also **run the tests/build** to confirm findings. Use the same launcher, but replace the sandbox config with:

```bash
-c 'sandbox_mode="workspace-write"'
```

`workspace-write` lets Codex write within the repo (run tests, build) but it cannot escape the working directory or reach arbitrary network. **Never use `--dangerously-bypass-approvals-and-sandbox` (`--yolo`, = `danger-full-access` + no approvals) for a review** — it removes the sandbox entirely (full write + network + command execution), defeating the point of a read-only reviewer. Reserve yolo for a deliberate _fix_ workflow, never this skill.

**The prompt's permission sentence must move with the sandbox flag.** The sandbox is what Codex _can_ do; the prompt is what it believes it _may_ do, and the two are set in different places. A prompt reused from an earlier read-only run still says "READ-ONLY. Do not edit, write, commit, or push" — under `workspace-write` that silently cancels the reason you opened the sandbox, and Codex declines the tests or mutation checks you just paid for. Nothing errors; the pass simply comes back thinner than it should, and the omission is invisible unless you demanded evidence the check ran.

Say exactly one of these, and pick it from the flag you actually passed:

| Sandbox           | Permission sentence in the prompt                                                                                                                           |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read-only`       | Read-only. Do not edit, write, commit, or push. Report findings only.                                                                                       |
| `workspace-write` | You may run tests and make temporary local edits to verify a finding. Restore every file before finishing, and leave the tree clean. Do not commit or push. |

Two things make the `workspace-write` wording load-bearing. Ask for the **restore** explicitly — a reviewer that mutates a file to prove an assertion is unpinned has no other reason to put it back, and you inherit a dirty tree you then have to untangle from your own edits. And ask for the **evidence** in the output contract — "state which checks you ran and what happened" — because a silent decline and a genuine clean pass produce the same terse "no material findings".

Start the run from a clean tree so anything dirty afterwards is unambiguously the reviewer's, and check `git status` before acting on the findings.

## Phase 3: Relay, verify, and record findings

When the status file appears with exit code zero, read the findings file — it holds just Codex's final message, no need to dig through the stream. Treat the findings as a **second opinion, not a verdict**:

- **First, confirm the run actually happened.** A safety refusal exits **zero**, so the status file cannot distinguish it from a clean review. A refused run looks like: an empty or missing findings file, or a findings file containing a decline ("I can't help with that", "I won't assist with…") instead of the contracted format, usually after a run far shorter than normal. **Never report that as "no material findings"** — it is an unreviewed change. Re-frame the prompt per the Phase 1 rules, keep the scope identical, and launch a fresh run; do not argue with the refusal in a follow-up turn on the same run. If a re-framed prompt is refused again, say so plainly and fall back to the Claude-side review chain rather than reporting the change as reviewed.
- For each finding, verify it against the actual source before acting — Codex can be confidently wrong, just like any reviewer.
- Deduplicate against the complete PR ledger by fingerprint and semantic defect.
- For each new confirmed finding, post an inline PR comment using the ledger marker **before editing the code**. Reuse an existing thread instead of opening a duplicate.
- Present the resulting thread list with `file:line`, severity, and your one-line verification.
- Call out where Codex **disagreed with or added to** earlier ledger findings;
  that cross-engine delta is the reason to run it.
- If Codex reports no new confirmed finding and makes no commit, post a
  clean-pass PR review attestation with `engine=codex` and the exact reviewed
  head. A fix pass attests through its thread replies instead.
- Your attestation stays valid while the head moves for **tests, fixtures,
  comments, or docs only** — those leave every production line you reviewed
  byte-identical. It is invalidated only by a change to product code. When
  reviewing a head that moved since your last attestation, diff the two over
  product paths first: if that diff is empty, carry the attestation forward and
  say so rather than re-reviewing unchanged product code as if it were new.
- If this pass changes no product code, stop the loop and recommend this repo's
  ship step, whatever it uses to merge the PR. A round that finds only test and
  comment work means the product converged and the review is auditing its own
  artifacts — a self-renewing surface, so the next round will find more and
  still not improve what ships.

## Phase 4: Disposition

Fix only **confirmed** findings (default: fix now, in this PR). Dismiss false positives by replying with evidence and resolving the thread.

In a convergence round the default inverts: fix only a blocking defect — wrong shipped behavior, data loss or corruption, a security or privacy hole, a broken public contract, or broken deploy/rollout — with the smallest edit that clears it. Every other confirmed finding gets an issue, an `outcome=deferred` reply with the link, and a resolved thread. Those deferrals are usually real findings; fixing them here just moves the head and buys another round.

For a finding that needs a human/scope/legal decision (risk acceptance, prod-data assumptions, an architectural rework), do not guess at the decision — but do disposition the thread, because convergence requires every marked thread to carry a reply and a resolution. File the tracking issue, reply with `outcome=deferred` plus the issue link, resolve the thread, and surface the decision to the user in the skill output. Leave the thread unresolved only when you cannot even file the issue; that is a non-converging run, so say so plainly and leave the PR in draft.

After fixes, run the relevant gates, commit, and push normally. Require local,
remote, and PR heads to match. Reply to each fixed thread with the commit SHA
and validation result plus the ledger's structured `outcome=fixed` marker, then
resolve it. After the last adversarial lane, post the committed-pass completion
marker for the exact before/final head pair. Do not force-push or merge.

## Output

End with:

```
✅ /codex-review complete (mode: <read-only | verify>).
- Scope: PR #N vs <base> at <reviewed-head>
- Round: <n> (<adversarial | convergence>)
- Codex findings: <total> (<confirmed>/<disputed>/<needs-human-decision>)
- Fixed: <count>  ·  Dismissed: <count>  ·  Flagged for you: <count>
- Threads: <posted>/<replied>/<resolved>
- Findings: /tmp/codex-findings-<run-id>.md  ·  full log: /tmp/codex-full-<run-id>.out
```

Add one line on the Claude-vs-Codex delta from the current ledger.

## Source of truth

This skill lives upstream at `.claude/skills/codex-review/`. Synced to consumer repos via the sync mechanism. Edits in a consumer will be overwritten — make changes upstream.
