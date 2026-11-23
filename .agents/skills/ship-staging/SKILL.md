---
name: ship-staging
description: Mark an approved draft pull request ready, merge it into a staging base branch, mark linked issues as on-staging, fast-forward the local staging reference checkout, and queue a short Google Chat notification for durable delivery. Use when the user asks to ship, merge, or deploy a staging-targeted PR through the staging branch, especially with "ship-staging".
---

# Ship Staging

Mark an approved draft PR ready when necessary, merge it into `staging`, mark
its linked issues as shipped to staging, refresh the local staging reference
checkout, and notify the configured development Google Chat space. This skill
is intentionally narrow: it ships staging-base PRs only.

## Guardrails

- Refuse when the PR base is not `staging`; production/main promotions need the
  repo's separate promotion or hotfix workflow.
- Merge with `gh pr merge <pr> --merge --delete-branch --match-head-commit "$APPROVED_HEAD"`.
- Never use `--admin`, `--squash`, `--rebase`, `--auto`, or `--no-verify`.
- Mark an open draft PR ready before evaluating its merge gates. Stop if the
  ready transition fails or cannot be verified.
- Stop on merge conflicts, blocked/dirty/unstable merge state, requested
  changes, failing CI, or checks still running.
- Resolve the Google Chat webhook at runtime from `GCHAT_DEV_WEBHOOK_URL` or a
  consumer-supplied path in `GCHAT_DEV_WEBHOOK_FILE`. The public skill does not
  define a default secret location.
- Treat the webhook URL as a secret: never print it, commit it, or include it in
  logs, PRs, issues, or summaries.
- Notification delivery never gates the merge. Record the notification intent
  in the durable outbox before merging; a missing webhook or a failed post is
  reported and left pending for a later `flush`, and never blocks or undoes the
  merge. Only an outbox that cannot record the intent stops before the merge.
- Require the repository label `status: on-staging` before merging so linked
  issues cannot silently remain in an actionable queue after the merge.

## Process

1. Parse the PR number from the first argument. Accept optional `--yes` only
   when the user explicitly requested an automatic path.
2. Locate the notification outbox and confirm it can be written. It lives in
   the private state directory (`$XDG_STATE_HOME`, else `~/.local/state`, under
   `activeloom/ship-staging/`), outside any worktree, and holds identifiers,
   the composed message, a request ID and an enum status only: never the
   webhook URL, its key or token, or a transport error. Do not resolve or
   print the webhook here; the helper reads it from `GCHAT_DEV_WEBHOOK_URL` or
   `GCHAT_DEV_WEBHOOK_FILE` only when it delivers.

   ```bash
   OUTBOX=.agents/skills/ship-staging/scripts/notification-outbox.py
   CURRENT_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner') || exit 1
   rc=0
   python3 "$OUTBOX" pending --repo "$CURRENT_REPO" || rc=$?
   if [[ $rc -ne 0 && $rc -ne 3 ]]; then
     echo "Notification outbox is unavailable; refusing to merge" >&2
     exit 1
   fi
   ```

   Exit 3 lists this repository's pending notifications. Step 11 refreshes
   that list after retrying confirmed intents. An
   `awaiting-merge` intent whose PR has since merged is never retried on its
   own: confirm it with `python3 "$OUTBOX" confirm --id <id> --merge <sha>`
   using the PR's verified `mergeCommit`, or `discard` it if the PR closed
   unmerged.

3. Fetch PR state. Refuse before changing draft state unless the PR is
   open and targets `staging`. If it is a draft, mark it ready, refetch the same
   fields, and verify that the PR is still open, still targets `staging`, has
   the same head, and is no longer a draft:

   ```bash
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

   # A successful lookup with no required checks is valid. The helper still
   # checks all available CI and refuses lookup errors or changed PR heads.
   APPROVED_HEAD=$(jq -er '.headRefOid' <<<"$PR_JSON") || exit 1
   python3 .agents/skills/ship-staging/scripts/check-pr-checks.py \
     --repo "$CURRENT_REPO" --pr <pr> \
     --head "$APPROVED_HEAD" || exit 1
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
   - `mergeStateStatus` is anything other than `CLEAN` or `HAS_HOOKS`
   - `reviewDecision == "CHANGES_REQUESTED"`
   - any status check conclusion is `FAILURE`, `CANCELLED`, `TIMED_OUT`, or
     `ACTION_REQUIRED`
   - the CI helper exits nonzero: required and available checks must pass or be
     skipped/neutral, with no failed, cancelled, or pending checks

   `mergeStateStatus` of `CLEAN` or `HAS_HOOKS` is acceptable when checks are
   otherwise green. No required checks is a valid repository configuration;
   report `required_count: 0` without requesting a bypass. This is distinct from
   no available CI evidence, which the helper refuses. A skipped/neutral check,
   including a required check, is acceptable only after the helper freshly reads
   `mergeable == "MERGEABLE"` and `mergeStateStatus` of `CLEAN` or `HAS_HOOKS`
   at the pinned head after checking CI. A changed head or branch, authentication,
   network, malformed-response, or other lookup error stops shipment. This does
   not replace the caller's local tests-at-head or promotion requirements.

5. Draft a Google Chat message. Keep it under 600 characters and at most three
   sentences after the header. Do not include PHI, secrets, internal IPs, commit
   SHAs, or reviewer/bot names.

   Template:

   ```text
   *Merged to staging:* PR #<number> - <title>

   <one-to-three sentence summary of what changed, why it matters, and caveats/follow-ups if any>

   <url>
   ```

   Write it to a private temporary file for the outbox:

   ```bash
   MESSAGE_FILE=$(mktemp /tmp/ship-staging-<pr>-message.XXXXXX)
   printf '%s\n' "$MESSAGE" > "$MESSAGE_FILE"
   ```

6. Unless `--yes` was explicitly requested, show the assembled message and exact
   merge command, then ask the user to choose ship, edit summary, or cancel.
7. Record the notification intent, then merge. The intent is keyed by
   `<repo>#<pr>` and keeps one request ID for its whole life, so re-running
   this step after an edited summary updates the same unmerged intent. If
   recording fails, stop without merging.

   ```bash
   if ! RECORD=$(python3 "$OUTBOX" record --repo "$CURRENT_REPO" --pr <pr> \
       --head "$APPROVED_HEAD" --message-file "$MESSAGE_FILE"); then
     echo "Could not record notification intent; refusing to merge" >&2
     exit 1
   fi
   NOTIFICATION_ID=$(jq -r '.id' <<<"$RECORD")
   rm -f "$MESSAGE_FILE"

   gh pr merge <pr> --merge --delete-branch --match-head-commit "$APPROVED_HEAD"
   ```

   A merge command that exits non-zero can still have merged (local branch
   cleanup fails after the server merge), so read the PR's `state` before
   treating it as a failure. If the PR did not merge, drop the intent with
   `python3 "$OUTBOX" discard --id "$NOTIFICATION_ID"`, do not label issues,
   and stop. A changed head requires fresh checks and summary approval.

8. Capture the merge commit and confirm the intent with it. Only a confirmed
   intent is ever delivered.

   ```bash
   MERGE_SHA=$(gh pr view <pr> --json mergeCommit --jq '.mergeCommit.oid')
   python3 "$OUTBOX" confirm --id "$NOTIFICATION_ID" --merge "$MERGE_SHA" \
     || echo "warning: could not confirm notification $NOTIFICATION_ID; it stays awaiting-merge" >&2
   ```

   A confirm failure does not undo or repeat the merge. Report it; re-run the
   same `confirm` command once the outbox is writable.

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

   Keep this phase best-effort: labeling failures must not block notification delivery
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
    report the result, but do not block notification delivery.

11. Deliver this repository's pending notifications, including any left by
    earlier ships. Read the stored status of this ship's intent for the report.

    ```bash
    python3 "$OUTBOX" flush --repo "$CURRENT_REPO" || :
    if STATUS=$(python3 "$OUTBOX" status --id "$NOTIFICATION_ID"); then
      NOTIFICATION=$(jq -er --arg id "$NOTIFICATION_ID" 'select(.id == $id) | .notification' <<<"$STATUS") || NOTIFICATION=outbox-error
    else
      NOTIFICATION=outbox-error
    fi
    ```

    Run `pending --repo "$CURRENT_REPO"` again through the outbox helper as
    in step 2. Report the remaining IDs other than `$NOTIFICATION_ID`. If the
    query fails, report the list as unavailable.

    The helper posts only to `https://chat.googleapis.com/v1/spaces/<space>/messages`,
    refuses redirects, sends the intent's request ID so the provider
    deduplicates a retried post, and marks the intent `delivered` only on a
    `spaces/<space>/messages/<id>` receipt. A delivered intent is never posted
    again, so `flush` is safe to run repeatedly. Anything else stays pending
    with an enum reason: `pending-configuration` (no valid webhook),
    `pending-delivery` (the post failed or returned no receipt), or
    `pending-destination-change` (an earlier uncertain attempt went to a
    different space; check that space, then `discard` the intent if it was
    posted or re-point the webhook to retry). Exit 3 means something is still
    pending; it is a notification follow-up, not a failed ship. Do not merge
    again or repeat issue changes to retry a notification; run
    `python3 "$OUTBOX" flush --repo "$CURRENT_REPO"` later.

12. Report:

    ```text
    Shipped PR #<number> - <title>
    Merge commit: <short-sha>
    Ready transition: <marked ready | already ready>
    Issue discovery: <complete | degraded | failed>
    Issues marked status: on-staging: <#N, #M | none>
    Staging checkout: <fast-forwarded | skipped with reason>
    Chat notification: <delivered | pending reason> (id <notification-id>)
    Other pending notifications: <ids | none>
    PR: <url>
    ```

    Do not claim delivery without a `delivered` status. Pending intents
    persist across sessions; never delete the outbox during worktree cleanup.

## Summary Guidance

Write for teammates skimming a dev channel. Prefer concrete user or operational
impact over implementation detail. Mention migrations, flags, follow-up issue
numbers, or manual validation only when relevant. Keep the PR link as the source
of traceability instead of listing commits.
