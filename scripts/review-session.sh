#!/usr/bin/env bash
# Repository-only standalone launch environment; never installed into user homes.
set -euo pipefail

case "${1:-}" in
  claude|codex|agy) ;;
  *)
    echo "usage: scripts/review-session.sh {claude|codex|agy} [arguments...]" >&2
    exit 2
    ;;
esac

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
checkout_root="$(git rev-parse --show-toplevel)"
if [[ "$(cd -- "$checkout_root" && pwd -P)" != "$script_root" ]]; then
  echo "review-session: run this checkout's script from within the same checkout" >&2
  exit 2
fi

# Match this repository's native Claude/Codex settings without changing shared
# skill bundles. Explicit environment values and emission/extraction gates stay.
export LOOM_REVIEW_TELEMETRY_STORE="${LOOM_REVIEW_TELEMETRY_STORE:-issue}"
exec "$@"
