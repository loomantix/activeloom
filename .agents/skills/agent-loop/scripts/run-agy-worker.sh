#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: $0 --model MODEL" >&2
    exit 2
}

model=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --model) [ "$#" -ge 2 ] || usage; model="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[ -n "$model" ] || usage
: "${AGENT_LOOP_PROMPT:?AGENT_LOOP_PROMPT is required}"

agy_cli="${AGY_CLI:-agy}"
command -v "$agy_cli" >/dev/null 2>&1 || { echo "agy is required" >&2; exit 1; }

result_file="$(mktemp)"
trap 'rm -f -- "$result_file"' EXIT
chmod 600 "$result_file"
agy_exit=0
"$agy_cli" \
    --model "$model" \
    --effort high \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --disable-slash-commands \
    --output-format json \
    --print-timeout 60m \
    --print "$AGENT_LOOP_PROMPT" >"$result_file" || agy_exit="$?"

python3 - "$result_file" "$agy_exit" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
exit_code = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"agy worker returned invalid JSON (exit {exit_code}): {error}")

status = payload.get("status")
if exit_code != 0 or status != "SUCCESS":
    detail = payload.get("response") or payload.get("error") or "no error detail"
    raise SystemExit(f"agy worker failed (exit {exit_code}, status {status!r}): {detail}")

response = payload.get("response")
if not isinstance(response, str) or not response.strip():
    raise SystemExit("agy worker succeeded without a text response")
print(response)
PY
