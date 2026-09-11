#!/usr/bin/env bash
set -euo pipefail

# checkout can replace an annotated tag ref with the event's peeled commit.
# Restore only the runner's local ref; never modify the remote release tag.
git fetch --force --no-tags origin \
  "refs/tags/${GITHUB_REF_NAME}:refs/tags/${GITHUB_REF_NAME}"
test "$(git cat-file -t "refs/tags/$GITHUB_REF_NAME")" = tag
test "$(git rev-list -n 1 "refs/tags/$GITHUB_REF_NAME")" = "$GITHUB_SHA"
