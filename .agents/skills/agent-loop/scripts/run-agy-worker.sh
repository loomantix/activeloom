#!/usr/bin/env bash
# Fail-closed launcher for the default Agy worker.
set -euo pipefail

# The wrapper passes the pinned Gemini worker model and effort from the review
# profile.
usage() {
    echo "usage: $0 --model MODEL --effort EFFORT" >&2
    exit 2
}

model=""
effort=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --model) [ "$#" -ge 2 ] || usage; model="$2"; shift 2 ;;
        --effort) [ "$#" -ge 2 ] || usage; effort="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[ -n "$model" ] && [ -n "$effort" ] || usage
: "${AGENT_LOOP_PROMPT:?AGENT_LOOP_PROMPT is required}"

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=run-agy-launch.sh
source "$SCRIPT_DIR/run-agy-launch.sh"

# claude-cli-invocations:start
run_agy_and_parse "agy worker" \
    --model "$model" \
    --effort "$effort" \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --disable-slash-commands \
    --output-format json \
    --print-timeout 60m \
    --print "$AGENT_LOOP_PROMPT"
# claude-cli-invocations:end
