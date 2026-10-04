#!/usr/bin/env bash
# Fail unless the Regression workflow has a successful run at exactly the given
# commit. Run before advancing a distribution tag: routine CI covers the fast
# lane and path-selected integration modules only, so a green `main` does not
# say the complete suite and its coverage floors passed.
#
# Usage: scripts/require-regression-green.sh <commit-sha>
#
# Exit codes:
#   0 = a successful Regression run exists at that commit
#   1 = none does (the message says how to start one)
#   2 = usage or environment problem

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <commit-sha>" >&2
  exit 2
fi
command -v gh >/dev/null || { echo "ERROR: gh is required." >&2; exit 2; }

sha="$(git rev-parse --verify "$1^{commit}")" || exit 2

run="$(gh run list --workflow regression.yml --commit "$sha" --status success \
  --limit 1 --json url --jq '.[0].url // empty')"

if [ -z "$run" ]; then
  echo "No successful Regression run at $sha." >&2
  echo "Start one on a ref whose head is that commit and wait for it:" >&2
  echo "  gh workflow run regression.yml --ref <branch>" >&2
  exit 1
fi
echo "Regression passed at $sha: $run"
