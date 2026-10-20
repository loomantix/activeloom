---
name: diagnosing-bugs
description: Disciplined diagnosis loop for hard bugs and performance regressions. Use when something is broken, throwing, failing, flaky, or slow, or when the user says debug, diagnose, or "why is this happening".
---

# Diagnosing bugs

A discipline for bugs that did not fall to the first read. The whole skill is Phase 1; everything after it is mechanical.

Skip a phase only when you can say why. Phase 0 applies to production incidents only.

## Handling what the loop captures

Diagnosis means running commands and capturing their output. Two things must never end up in what you show, save, log, or paste into an issue or PR:

- **Secrets.** Write `<REDACTED>` in their place. Build loops against environment variables so the credential stays in the environment rather than in the command you display. Captured artifacts carry auth headers — quote only the lines carrying signal.
- **Regulated and user-authored content.** Never user-entered text, transcribed or dictated text, model-generated content, or person-identifying fields. This is default-deny, not a denylist: quote opaque IDs, enums, counts, timings and error codes, and drop everything else. Prose under an innocuous key still leaks — a denylist of field names cannot catch it.

Two consequences worth stating outright, because they are where this usually goes wrong:

- **Build the repro on synthetic data.** A fixture that reproduces the bug is a better artifact than a real record anyway — it is redistributable, it can go in the regression test, and it cannot leak.
- **Temporary production instrumentation is the last resort, and it logs identifiers only.** Adding a log line that dumps a free-form value into a telemetry sink is the exact failure a default-deny logging rule exists to prevent. If the diagnosis genuinely requires seeing a real value, that is a conversation with the user, not a patch.

If redaction leaves you without enough signal to diagnose, say so and ask.

## Phase 0 — production incident: reconstruct it before you reproduce it

Applies when the bug is something that happened to a real user and all you hold is telemetry. Skip it when you can already trigger the bug at will.

The error message is where the failure surfaced, not where it began. Build the loop from the error string alone and you reproduce the nearest bug with that message — often one that already has a passing test.

Before Phase 1, write the incident as a timeline from evidence (logs, error tracker, request logs), one machine and one session at a time:

- **Split by machine and session first.** One user ID can be several browsers on several machines. A timeline that merges them produces false causes.
- **Work backwards from every duration in the code.** For a timeout observed at T with a 15 s budget, T − 15 s is a starting estimate: scheduling delays or suspension can put the actual start earlier. Corroborate it with timer creation timestamps and lifecycle evidence before assigning the failure to an older object.
- **List every fact that does not fit** — a wrong ID on the event, a request against an unrelated record, a window that closed before the error. These are the leads. Do not file one as a separate cosmetic bug until the diagnosis explains it.
- **Note what never happened.** A missing request is evidence: name the call the happy path makes and confirm whether it was made.
- **A matching open issue is a hypothesis, not a diagnosis.** Read it to the end. Check whether its fix reached the incident's actual build and code path, and whether it covers the observed sequence. A shipped fix can be absent, incomplete or regressed; rule out the original cause with evidence.

Quote the timeline under the same rules as everything else the loop captures: opaque IDs, timings and error codes, never user content.

Reading code in this phase is allowed for one purpose: learning what a log line means and what durations the code uses. Theories about the cause still wait for the loop.

Phase 0 is done when the timeline says, in order, what the user and the system did, and the Phase 1 loop can be written to **replay that sequence** rather than to provoke that error.

## Phase 1 — build a feedback loop

**This is the skill.** If you have a **tight** pass/fail signal that goes **red** on _this_ bug, you will find the cause — bisection, hypothesis testing and instrumentation all just consume it. Without one, no amount of reading code will save you.

Spend disproportionate effort here. Be aggressive, be creative, refuse to give up.

### Ways to build one, roughly in order

1. **Failing test** at whatever seam reaches the bug.
2. **HTTP script** against a running dev server.
3. **CLI invocation** with a fixture input, diffed against known-good output.
4. **Headless browser script** driving the UI, asserting on DOM, console or network.
5. **Replay a captured payload** — save a request or event to disk (redacted, synthetic where possible) and replay it through the code path in isolation.
6. **Throwaway harness** — a minimal subset of the system, dependencies mocked, that hits the bug path in one function call.
7. **Property or fuzz loop** — for "sometimes wrong output", run many random inputs and look for the failure mode.
8. **Bisection harness** — if it appeared between two known states, automate "boot at state X, check, repeat" so `git bisect run` can drive it.
9. **Differential loop** — same input through two versions or two configs, diff the outputs.
10. **Scripted manual loop** — last resort, when a human must click. Give them an exact numbered script and a precise description of what to capture, so the result still comes back as structured evidence rather than an impression.

### Tighten it

Once you have _a_ loop, treat it as a product and make it **tight**:

- Faster — cache setup, skip unrelated init, narrow the scope.
- Sharper — assert on the specific symptom, not "did not crash".
- More deterministic — pin time, seed randomness, isolate the filesystem, freeze the network.

A 30-second flaky loop is barely better than nothing. A 2-second deterministic one is a superpower.

### Non-deterministic bugs

The goal is not a clean repro, it is a **higher reproduction rate**. Loop the trigger, parallelise, add stress, narrow timing windows, inject sleeps. A 50%-flake bug is debuggable; a 1% one is not. Keep raising the rate until it is.

### Phase 1 is done when

You can name **one command** you have **already run at least once** — show the invocation and its output, redacted — that is:

- [ ] **Red-capable** — it drives the real bug path and asserts the **user's exact symptom**, so it goes red on this bug and green once fixed. Not "runs without erroring".
- [ ] **Deterministic** — same verdict every run, or a pinned high reproduction rate.
- [ ] **Fast** — seconds, not minutes.
- [ ] **Runnable unattended.**

If you catch yourself reading code to build a theory before that command exists, stop. Jumping to a hypothesis is the exact failure this skill prevents. No red-capable command, no Phase 2.

### When you genuinely cannot build one

Stop and say so. List what you tried, and ask for one of: access to an environment that reproduces it, a redacted artifact (log dump, HAR, core dump, screen recording with timestamps), or a decision about instrumentation. Proceed to hypothesising without a loop and you are guessing with extra steps.

## Phase 2 — reproduce, then minimise

Run the loop. Watch it go red. Confirm:

- [ ] It produces the failure **the user described**, not a different one nearby. Wrong bug, wrong fix.
- [ ] It reproduces across runs (or at a high enough rate to debug against).
- [ ] You have captured the exact symptom, so later phases can prove the fix addressed it.

Then **minimise**: shrink to the smallest scenario that still goes red. Cut inputs, callers, config, data and steps **one at a time**, re-running after each cut.

This is not tidiness. A minimal repro shrinks the hypothesis space in Phase 3 and becomes the regression test in Phase 5.

Done when **every remaining element is load-bearing** — removing any one turns the loop green.

## Phase 3 — hypothesise

Generate **3–5 ranked hypotheses before testing any of them.** Generating one at a time anchors you on the first plausible idea.

Each must be **falsifiable** — state the prediction:

> If X is the cause, then changing Y makes the bug disappear / changing Z makes it worse.

If you cannot state the prediction, it is a vibe. Sharpen it or drop it.

**Show the ranked list before testing.** The user often re-ranks it instantly ("we deployed a change to #3 yesterday") or has already ruled one out. Cheap checkpoint, large payoff. Do not block on it — proceed with your ranking if they are away.

## Phase 4 — instrument

Every probe maps to a specific prediction from Phase 3. **Change one variable at a time.**

1. **Debugger or REPL** where the environment supports it — one breakpoint beats ten logs.
2. **Targeted logs** at the boundaries that distinguish the hypotheses.
3. Never "log everything and grep".

**Tag every debug log with a unique prefix** — `[DEBUG-a4f2]`. Cleanup becomes one grep. Untagged logs survive forever; tagged logs die.

**Performance regressions branch here.** Logs are usually the wrong tool. Establish a baseline measurement — timing harness, profiler, query plan — then bisect against it. Measure first, fix second.

## The root-cause bar

Check this before Phase 5, and before reporting a cause to anyone. "Root cause" is claimed only when all four hold:

- [ ] **Reproduced** — the loop goes red on the user's exact symptom; for an incident, on the incident's own sequence from Phase 0.
- [ ] **Flipped** — changing one variable turns it green, and reverting turns it red again. Tag that experiment `[DEBUG-...]`; it is a probe, not the fix.
- [ ] **Nothing left over** — every observed fact, including every item on the Phase 0 list, is explained by the mechanism. One unexplained fact means there is a second cause or the wrong one.
- [ ] **Three answers, kept separate** — the _mechanism_ (what in the code turns the trigger into the failure), the _trigger_ (what the user or system did to reach it), and _why nothing caught it_ (the missing guard, test or alert).

Report each of the three as proven, inferred or unknown, in those words. A proven mechanism with an unknown trigger is a legitimate place to stop, provided the report says so and names the evidence that would settle it. A cause that is only consistent with the logs is a hypothesis; say "consistent with", not "caused by".

## Phase 5 — fix, with a regression test

Write the regression test **before** the fix, if there is a **correct seam** for it.

A correct seam exercises the real bug pattern as it occurs at the call site. If the only reachable seam is too shallow — a single-caller test when the bug needs several callers, a unit test that cannot reproduce the triggering chain — a test there gives false confidence.

**If no correct seam exists, that is itself the finding.** Note it: the architecture is preventing the bug from being locked down.

Where a seam exists:

1. Turn the minimised repro into a failing test there.
2. Watch it fail.
3. Apply the fix.
4. Watch it pass.
5. Re-run the Phase 1 loop against the original, un-minimised scenario.

## Phase 6 — clean up and close out

Before declaring done:

- [ ] The original repro no longer reproduces — re-run the Phase 1 loop.
- [ ] The regression test passes, or the absence of a seam is written down.
- [ ] Every `[DEBUG-...]` line is gone — grep the prefix.
- [ ] Throwaway harnesses deleted, or moved somewhere clearly marked.
- [ ] No captured artifact containing real data survives in the working tree.
- [ ] The hypothesis that turned out correct is stated in the commit or PR body, so the next person debugging this learns something.

**When the fix is not yours to make** — it needs a design decision, a different review tier, or sign-off — stop after the root-cause bar and leave three things: the red test (uncommitted or on a branch, never merged red), the experiment removed from source and kept as a patch, and a write-up of mechanism, trigger and why-not-caught with the candidate fixes. Say plainly that nothing is fixed.

**Then ask what would have prevented it.** If the answer is a missing test seam, tangled callers, or hidden coupling, that is a cleanup with a scope of its own — file it, or hand it to `/refactorpass` on the follow-up PR with the specifics. Make that call **after** the fix is in; you know more now than when you started.

---

Adapted from the `diagnosing-bugs` skill in [mattpocock/skills](https://github.com/mattpocock/skills) (MIT), with default-deny telemetry rules replacing its generic redaction guidance — see [NOTICE](../../../NOTICE).
