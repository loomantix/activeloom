# shellcheck shell=bash
# Shared "invoke agy, capture JSON, enforce the SUCCESS contract" helper.
# Sourced only by run-agy-worker.sh and run-agy-review.sh; not directly executable.

run_agy_and_parse() {
    local label="$1"
    shift
    local agy_cli="${AGY_CLI:-agy}"
    command -v "$agy_cli" >/dev/null 2>&1 || { echo "agy is required" >&2; return 1; }

    AGY_LAUNCH_RESULT_FILE="$(mktemp)"
    trap 'rm -f -- "${AGY_LAUNCH_RESULT_FILE:-}"' EXIT
    chmod 600 "$AGY_LAUNCH_RESULT_FILE"

    local agy_exit=0
    "$agy_cli" "$@" >"$AGY_LAUNCH_RESULT_FILE" || agy_exit="$?"

    AGY_LAUNCH_LABEL="$label" python3 - "$AGY_LAUNCH_RESULT_FILE" "$agy_exit" <<'PY'
import json
import os
import pathlib
import sys

label = os.environ["AGY_LAUNCH_LABEL"]
path = pathlib.Path(sys.argv[1])
exit_code = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"{label} returned invalid JSON (exit {exit_code}): {error}")

status = payload.get("status")
if exit_code != 0 or status != "SUCCESS":
    detail = payload.get("response") or payload.get("error") or "no error detail"
    raise SystemExit(f"{label} failed (exit {exit_code}, status {status!r}): {detail}")

response = payload.get("response")
if not isinstance(response, str) or not response.strip():
    raise SystemExit(f"{label} succeeded without a text response")
print(response)
PY
}
