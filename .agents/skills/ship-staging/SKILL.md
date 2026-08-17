---
name: ship-staging
description: Mark an approved draft pull request ready, merge it into a staging base branch, mark linked issues as on-staging, fast-forward the local staging reference checkout, and post a short Google Chat notification. Use when the user asks to ship, merge, or deploy a staging-targeted PR through the staging branch, especially with "ship-staging".
---

# Ship Staging

Mark an approved draft PR ready when necessary, merge it into `staging`, mark
its linked issues as shipped to staging, refresh the local staging reference
checkout, and notify the configured development Google Chat space. This skill
is intentionally narrow: it ships staging-base PRs only.

## Guardrails

- Refuse when the PR base is not `staging`; production/main promotions need the
  repo's separate promotion or hotfix workflow.
- Use only `gh pr merge <pr> --merge --delete-branch`.
- Never use `--admin`, `--squash`, `--rebase`, `--auto`, or `--no-verify`.
- Mark an open draft PR ready before evaluating its merge gates. Stop if the
  ready transition fails or cannot be verified.
- Stop on merge conflicts, blocked/dirty/unstable merge state, requested
  changes, failing CI, or required checks still running.
- Resolve the Google Chat webhook at runtime from `GCHAT_DEV_WEBHOOK_URL` or a
  consumer-supplied path in `GCHAT_DEV_WEBHOOK_FILE`. The public skill does not
  define a default secret location.
- Treat the webhook URL as a secret: never print it, commit it, or include it in
  logs, PRs, issues, or summaries.
- Do not merge if the webhook secret is unavailable; the notification is part of
  the staging ship contract.
- Require the repository label `status: on-staging` before merging so linked
  issues cannot silently remain in an actionable queue after the merge.

## Process

1. Parse the PR number from the first argument. Accept optional `--yes` only
   when the user explicitly requested an automatic path.
2. Resolve the webhook secret before validating the PR:

   ```bash
   if [[ -n "$GCHAT_DEV_WEBHOOK_URL" ]]; then
     GCHAT_WEBHOOK_URL="$GCHAT_DEV_WEBHOOK_URL"
   elif [[ -n "$GCHAT_DEV_WEBHOOK_FILE" ]]; then
     if [[ ! -f "$GCHAT_DEV_WEBHOOK_FILE" || ! -r "$GCHAT_DEV_WEBHOOK_FILE" ]]; then
       echo "GCHAT_DEV_WEBHOOK_FILE must name a readable regular file" >&2
       exit 1
     fi
     GCHAT_WEBHOOK_URL=$(<"$GCHAT_DEV_WEBHOOK_FILE")
   else
     echo "Missing webhook secret: set GCHAT_DEV_WEBHOOK_URL or GCHAT_DEV_WEBHOOK_FILE" >&2
     exit 1
   fi
   if [[ -z "${GCHAT_WEBHOOK_URL//[[:space:]]/}" ]]; then
     echo "Webhook secret is empty" >&2
     exit 1
   fi
   ```

3. Resolve the current repository identity and fetch PR state. Both queries must
   succeed before merging. Refuse before changing draft state unless the PR is
   open and targets `staging`. If it is a draft, mark it ready, refetch the same
   fields, and verify that the PR is still open, still targets `staging`, has
   the same head, and is no longer a draft:

   ```bash
   CURRENT_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner') || exit 1
   if ! PR_JSON=$(gh pr view <pr> --json number,title,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,url,body,author,reviewDecision,statusCheckRollup,labels); then
     echo "Could not resolve PR state; refusing to merge" >&2
     exit 1
   fi
   if ! jq -e '.state == "OPEN" and .baseRefName == "staging"' <<<"$PR_JSON" >/dev/null; then
     echo "PR must be open and target staging; refusing to change readiness" >&2
     exit 1
   fi

   READY_TRANSITION="already ready"
   if jq -e '.isDraft == true' <<<"$PR_JSON" >/dev/null; then
     PRE_READY_HEAD=$(jq -r '.headRefOid' <<<"$PR_JSON")
     if ! gh pr ready <pr>; then
       echo "Could not mark draft PR ready; refusing to merge" >&2
       exit 1
     fi
     if ! PR_JSON=$(gh pr view <pr> --json number,title,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,url,body,author,reviewDecision,statusCheckRollup,labels); then
       echo "Could not verify PR after marking it ready; refusing to merge" >&2
       exit 1
     fi
     if ! jq -e --arg head "$PRE_READY_HEAD" '
       .state == "OPEN"
       and .baseRefName == "staging"
       and .headRefOid == $head
       and .isDraft == false
     ' <<<"$PR_JSON" >/dev/null; then
       echo "PR state or head changed during the ready transition; refusing to merge" >&2
       exit 1
     fi
     READY_TRANSITION="marked ready"
   fi

   checks_status=0
   REQUIRED_CHECKS=$(gh pr checks <pr> --required --json bucket,name,state) || checks_status=$?
   if [[ "$checks_status" -ne 0 && "$checks_status" -ne 8 ]]; then
     echo "Could not resolve required checks; refusing to merge" >&2
     exit 1
   fi
   # Hard gate, not just prose: exit code 8 means required checks are still
   # PENDING (gh reports pending as non-zero but still emits the JSON), so the
   # bucket rule below must be enforced in code. Refuse unless every required
   # check is in the `pass` bucket. An empty set is vacuously true here, but a
   # repo with no required checks returns exit 1 above and never reaches this.
   if ! jq -e 'all(.[]; .bucket == "pass")' <<<"${REQUIRED_CHECKS:-[]}" >/dev/null; then
     echo "Required checks are not all green (pending, failing, or skipped); refusing to merge" >&2
     exit 1
   fi
   ON_STAGING_LABEL=$(gh label list --search "status: on-staging" --limit 100 \
     --json name --jq 'any(.[]; .name == "status: on-staging")')
   if [[ "$ON_STAGING_LABEL" != "true" ]]; then
     echo "Required label status: on-staging is missing; create it before shipping" >&2
     exit 1
   fi
   ```

4. Refuse without merging when any of these are true:
   - `state != "OPEN"`
   - `isDraft != false` after the ready transition
   - `baseRefName != "staging"`
   - `mergeable != "MERGEABLE"`
   - `mergeStateStatus` is `BLOCKED`, `BEHIND`, `DIRTY`, or `UNSTABLE`
   - `reviewDecision == "CHANGES_REQUESTED"`
   - any status check conclusion is `FAILURE`, `CANCELLED`, `TIMED_OUT`, or
     `ACTION_REQUIRED`
   - any item in `REQUIRED_CHECKS` has a `bucket` other than `pass`

   `mergeStateStatus` of `CLEAN` or `HAS_HOOKS` is acceptable when checks are
   otherwise green.

5. Draft a Google Chat message. Keep it under 600 characters and at most three
   sentences after the header. Do not include PHI, secrets, internal IPs, commit
   SHAs, or reviewer/bot names.

   Template:

   ```text
   *Merged to staging:* PR #<number> - <title>

   <one-to-three sentence summary of what changed, why it matters, and caveats/follow-ups if any>

   <url>
   ```

   Save it as JSON:

   ```bash
   jq -n --arg text "$MESSAGE" '{text: $text}' > /tmp/ship-staging-<pr>-payload.json
   ```

6. Unless `--yes` was explicitly requested, show the assembled message and exact
   merge command, then ask the user to choose ship, edit summary, or cancel.
7. Merge:

   ```bash
   gh pr merge <pr> --merge --delete-branch
   ```

   If the merge fails, do not post to chat and do not label issues.

8. Capture the merge commit:

   ```bash
   MERGE_SHA=$(gh pr view <pr> --json mergeCommit --jq '.mergeCommit.oid')
   ```

9. Mark linked open issues as on-staging. A closing keyword on a staging PR does
   not close the issue until the work later reaches the default branch, so open
   fixed issues need an explicit "done, awaiting promotion" label in the
   meantime. Prefer GitHub's parsed closing references; fall back to explicit
   `Closes #N`, `Fixes #N`, or `Resolves #N` text in the PR title/body.

   ````bash
   ISSUE_DISCOVERY="complete"
   if refs_file=$(mktemp /tmp/ship-staging-<pr>-refs.XXXXXX.json); then
     if gh pr view <pr> --json closingIssuesReferences > "$refs_file"; then
       if ! LINKED=$(jq -r --arg repo "$CURRENT_REPO" '
         .closingIssuesReferences[]
         | select((.repository.owner.login + "/" + .repository.name) == $repo)
         | .number
       ' "$refs_file"); then
         echo "warning: could not parse closing references; trying PR text" >&2
         ISSUE_DISCOVERY="degraded"
         LINKED=""
       fi
     else
       echo "warning: could not read parsed closing references; trying PR text" >&2
       ISSUE_DISCOVERY="degraded"
       LINKED=""
     fi
     rm -f "$refs_file"
   else
     echo "warning: could not create temporary file for closing references; trying PR text" >&2
     ISSUE_DISCOVERY="degraded"
     LINKED=""
   fi

   if [[ -z "$LINKED" ]]; then
     if PR_TITLE=$(gh pr view <pr> --json title --jq '.title') && \
        PR_BODY=$(gh pr view <pr> --json body --jq '.body'); then
       TITLE_LINKED=$(printf '%s\n' "$PR_TITLE" \
         | grep -ioE '(^|[^[:alnum:]_])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]:]+#[0-9]+' \
         | grep -oE '[0-9]+' || true)
       BODY_LINKED=$(printf '%s\n' "$PR_BODY" \
         | awk '
             /^[[:space:]]*(```|~~~)/ { fenced = !fenced; next }
             fenced { next }
             in_comment { if ($0 ~ /-->/) in_comment = 0; next }
             /^[[:space:]]*<!--/ { if ($0 !~ /-->/) in_comment = 1; next }
             /^[[:space:]]*>/ { next }
             { print }
           ' \
         | grep -ioE '^[[:space:]]*(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]:]+#[0-9]+' \
         | grep -oE '[0-9]+' || true)
       LINKED=$(printf '%s\n%s\n' "$TITLE_LINKED" "$BODY_LINKED" \
         | sed '/^$/d' \
         | sort -u)
     else
       echo "warning: could not enumerate linked issues; none will be labeled" >&2
       ISSUE_DISCOVERY="failed"
       LINKED=""
     fi
   fi

   ISSUES_MARKED=()
   for n in $LINKED; do
     state=$(gh issue view "$n" --json state --jq '.state' 2>/dev/null) || {
       echo "warning: could not read issue #$n; leaving labels unchanged" >&2
       continue
     }
     [[ "$state" == "OPEN" ]] || continue

     if ! labels=$(gh issue view "$n" --json labels --jq '.labels[].name'); then
       echo "warning: could not read labels for issue #$n; leaving labels unchanged" >&2
       continue
     fi
     mapfile -t issue_labels <<< "$labels"
     edit_args=(--add-label "status: on-staging")
     if printf '%s\n' "${issue_labels[@]}" | grep -Fxq "dev: agent"; then
       edit_args+=(--remove-label "dev: agent")
     fi

     if gh issue edit "$n" "${edit_args[@]}"; then
       ISSUES_MARKED+=("#$n")
       echo "  marked #$n status: on-staging"
     else
       echo "warning: could not mark issue #$n status: on-staging" >&2
     fi
   done
   ````

   Keep this phase best-effort: labeling failures must not block the chat post
   because the PR has already merged. Do not close the issues. Do not add
   unrelated workflow labels. `status: on-staging` is the only label this skill
   adds. Remove only the agent-loop admission label `dev: agent` when it is
   present.

10. Fast-forward the local staging reference checkout. This keeps new worktrees
    and later staging/promotion diffs from starting from a stale local branch.
    The checkout can be overridden with `SHIP_STAGING_MAIN_CHECKOUT`; otherwise
    default to `$HOME/<repo-name>`, derived from the current repository's
    `origin` URL. Before fetching, verify that this checkout resolves to the
    same `nameWithOwner` as the PR repository; a same-named fork is different.

    ```bash
    current_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
    origin_url=$(git -C "$current_root" remote get-url origin 2>/dev/null || true)
    repo_name=$(basename "$origin_url")
    repo_name="${repo_name%.git}"
    MAIN_CHECKOUT="${SHIP_STAGING_MAIN_CHECKOUT:-}"
    if [[ -z "$MAIN_CHECKOUT" && -n "$repo_name" && "$repo_name" != "." ]]; then
      MAIN_CHECKOUT="$HOME/$repo_name"
    fi
    if [[ -z "$MAIN_CHECKOUT" ]]; then
      MAIN_CHECKOUT="$current_root"
    fi

    if ! git -C "$MAIN_CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "  (skip ff: $MAIN_CHECKOUT is not a git checkout)"
    elif ! checkout_repo=$(cd "$MAIN_CHECKOUT" && gh repo view --json nameWithOwner --jq '.nameWithOwner'); then
      echo "  (skip ff: could not resolve repository identity for $MAIN_CHECKOUT)"
    elif [[ "$checkout_repo" != "$CURRENT_REPO" ]]; then
      echo "  (skip ff: $MAIN_CHECKOUT belongs to a different repository)"
    elif [[ "$(git -C "$MAIN_CHECKOUT" rev-parse --abbrev-ref HEAD)" != "staging" ]]; then
      echo "  (skip ff: $MAIN_CHECKOUT is not on staging)"
    elif [[ -n "$(git -C "$MAIN_CHECKOUT" status --porcelain)" ]]; then
      echo "  (skip ff: $MAIN_CHECKOUT has uncommitted changes)"
    elif ! git -C "$MAIN_CHECKOUT" fetch origin staging --quiet; then
      echo "  (skip ff: $MAIN_CHECKOUT could not fetch origin/staging)"
    elif git -C "$MAIN_CHECKOUT" merge --ff-only origin/staging --quiet 2>/dev/null; then
      echo "  fast-forwarded $MAIN_CHECKOUT to origin/staging"
    else
      echo "  (skip ff: $MAIN_CHECKOUT could not fast-forward)"
    fi
    ```

    Fast-forward only. Never use merge commits, rebase, checkout overwrite, or
    `reset --hard` for this cleanup. Keep it best-effort like issue labeling:
    report the result, but do not block the chat post.

11. Post to Google Chat:

    ```bash
    RESPONSE=$(curl -sS -w "\n%{http_code}" \
      -X POST \
      -H "Content-Type: application/json" \
      --data @/tmp/ship-staging-<pr>-payload.json \
      "$GCHAT_WEBHOOK_URL")
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')
    MSG_ID=$(echo "$BODY" | jq -r '.name // empty')
    ```

    Success requires `HTTP_CODE == 200` and a non-empty message id shaped like
    `spaces/<space-id>/messages/<msg-id>`. If Google Chat returns `429`, wait
    30 seconds and retry once. If posting still fails, report that the PR merged
    but notification failed and leave the payload file in `/tmp` for manual
    re-posting.

12. On successful chat post, remove the temp payload file and report:

    ```text
    Shipped PR #<number> - <title>
    Merge commit: <short-sha>
    Ready transition: <marked ready | already ready>
    Issue discovery: <complete | degraded | failed>
    Issues marked status: on-staging: <#N, #M | none>
    Staging checkout: <fast-forwarded | skipped with reason>
    Chat notification: posted (message id <msg-id>)
    PR: <url>
    ```

## Summary Guidance

Write for teammates skimming a dev channel. Prefer concrete user or operational
impact over implementation detail. Mention migrations, flags, follow-up issue
numbers, or manual validation only when relevant. Keep the PR link as the source
of traceability instead of listing commits.
