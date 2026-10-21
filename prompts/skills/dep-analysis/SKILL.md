---
name: dep-analysis
description: Dependency analysis for a dependency update — what could break, the existing tests that exercise it (run), the signal that would show a break after merge, and a verdict recorded at the exact head. Use when a review entry point or herd skill hands over a range that `classify-changeset` reports as `dependencyUpdate`, for the dependency part of a mixed range, or when the user asks to analyse a dependency bump, Dependabot or Renovate PR.<<DESC_TRIGGER>>
<<FM_EXTRAS>>
---

# <<INVOKE>>dep-analysis — dependency analysis in place of a review chain

Usage: `<<INVOKE>>dep-analysis [<pr>]` (no argument: the current branch's open PR, or the branch against the default branch when it has none).

A dependency update — a manifest or lockfile change, a workspace catalog entry, a GitHub Actions version or SHA pin, a container image tag or digest — changes code this repository does not contain. A review chain has nothing in the diff to critique, so no review chain runs on one. This analysis asks the questions that decide whether the bump is safe: what changed upstream, what here consumes it, whether the tests that exercise those paths pass, and what would show a break after merge.

It writes no ledger run, roster, tier marker, attestation, or review telemetry record. Its one record is the result comment in [Report](#5-report).

## Scope

`L` = `<<SKILLS_ROOT>>/critique/scripts/review-ledger.js`.

- **Dependency update** — `classify-changeset` reports `"dependencyUpdate": true`. The whole range is analysed.
- **Mixed range** — `dependencyUpdate` is false and `dependencyPaths` is non-empty. The code part follows the normal review tier; this analysis covers only `dependencyPaths`. Run it after the chain converges, at the head the chain converged on, so the result names the head that ships.
- **Neither** — no dependency pin in the range: say so, hand back to the review entry point, and stop.

The classifier decides the route. When it reports a file as a pin change that is not one, or misses one, continue on its answer and open an issue naming the path and the rule.

## Steps

### 1. Pin the range

Read the PR (`gh pr view <pr> --json number,state,baseRefName,headRefName,headRefOid,isDraft`) and use full SHAs. Fetch the base and PR head (`git fetch origin <base-branch> <head-branch>`), check out or verify the worktree is at `<head>` (`git rev-parse HEAD` must equal `<head>`), then:

```bash
base_sha=$(git merge-base "origin/<base-branch>" <head>)
node $L classify-changeset --base "$base_sha" --head <head>
```

Done when the scope above is settled, local `HEAD` equals `<head>`, and the list of files under analysis is fixed.

### 2. What could break

For each dependency the range changes — every direct entry, and every transitive entry in the lockfile that moves a major version, changes source or registry, or adds an install script:

- **From → to.** The exact versions, tags, SHAs or digests on both sides.
- **What changed upstream.** Release notes or changelog for every version in between; breaking changes, deprecations and removals; security advisories fixed or introduced. A major bump, or a minor bump below 1.0, is presumed breaking until the notes say otherwise. For an Actions pin, the tag or commit range of the action and any change to its inputs, outputs or runtime. For a container image, the base OS and runtime versions inside the tag or digest, and whether the tag moved to a new digest.
- **Who consumes it.** Search this repository for imports, call sites, configuration, workflows that use the action, and Dockerfiles, compose files, manifests and deploys that use the image. Name the tier each consumer sits in: build or CI only, development tooling, runtime of a service, a deployed image.

Done when every changed dependency has a from → to, an upstream summary with its source, and a consumer list (or "no consumer found", with the search that showed it).

### 3. Tests

Find the existing tests that exercise the consumer paths from step 2: unit, integration, smoke, end-to-end. Verify `git rev-parse HEAD` equals `<head>` and `git status --porcelain` is empty, then run them at `<head>`, in a clean worktree on the PR branch, with the repository's own gate commands for each touched tier as <<AGENT_DOC>> and the CI workflows define them — a frozen install first, then the typecheck, build and test commands those tiers use. A tier that cannot run locally (a deployed end-to-end suite) is covered by a CI run at the exact head; name the run.

Record each command, its exit status, and the failures it printed. Read every failure before calling it unrelated: a dependency bump changes behaviour without changing a line here.

Done when each consumer path has either a passing test that exercises it, a failing one, or the entry "no test exercises this path".

### 4. Telemetry

Name, concretely, the signal that would show this bump broke something after merge: the CI job, the deploy smoke or end-to-end step, the error-tracker alert or project, the log event, the dashboard panel. "Monitoring" or "the logs" is not a signal. A consumer path with no signal that would fire on its failure is a gap.

Done when each consumer path has a named signal or is recorded as a gap.

### 5. Report

Pick the verdict:

- `clear` — no unaddressed breaking change, every consumer path is exercised by a test that passed at the head, and each has a named signal.
- `gaps` — nothing failed, but a consumer path has no exercising test or no signal. Recommend a separate issue documenting each gap, to close before merge.
- `broken` — a test failed, or an upstream breaking change reaches a consumer here without a fix in the range.
- `blocked` — the analysis could not finish: tests could not run, or upstream notes could not be read.

Post one PR comment, under 400 words, written to a scratch file and posted with `gh pr comment <pr> --body-file <file>`. Before posting, verify both `git rev-parse HEAD` and `gh pr view <pr> --json headRefOid -q .headRefOid` still equal `<head>` (if either moved, start over from step 1 and do not post a comment for a stale or unverified SHA). Its first line is the marker, with the exact head the analysis ran at:

```text
<!-- dependency-analysis:v1 head=<head sha> verdict=<clear|gaps|broken|blocked> -->
```

Then: the verdict in one line; per dependency, from → to, the breaking changes and security fixes that matter, and its consumers by tier; the test commands with their results; the named signals; and, for `gaps`, the gap list worded so it can be pasted into the recommended issue.

Without a PR, print the same report in the session instead of posting it.

Done when the comment is posted and its marker names the PR's current head (re-read `headRefOid`; a moved head means start over from step 1).

### 6. Ready

For a dependency update (not a mixed range) with verdict `clear`, wait for every CI run at the head to complete (`gh run list --commit <head>`). When there is at least one run and every one ended `success` or `skipped`, mark a draft PR ready with `gh pr ready <pr>`, unless the invocation said to leave it in draft. For a mixed range the review chain owns readiness. Any other verdict leaves the PR as it is.

Report under 150 words: verdict, head, dependencies analysed, test commands and results, signals or gaps, and whether the PR was marked ready.

## Rules

- No review chain on a dependency update, on any engine — not on request, not as a second opinion. A human who wants more than this analysis asks for the dependency to be split out or the change re-scoped; the classifier still decides the route.
- No merge. Readiness is the only PR state this skill changes.
- The analysis names its sources: a release-note link, an advisory identifier, a test command. A claim without one is not evidence for `clear`.
