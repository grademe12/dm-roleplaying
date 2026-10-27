#!/usr/bin/env bash
# Runtime hook: credentials stay in the separate receive service.
set -euo pipefail
source "${HATCH_HOOK_RUNTIME:?hook runtime helper is required}"

CONFIG=${CONTROLLER_AUTOMATION_CONFIG:-/opt/persona-controller/controller/automation.json}
PYTHON=${CONTROLLER_PYTHON:-python3}
SOURCE=${CONTROLLER_SOURCE:-/opt/persona-controller/controller}
dry_args=()
if [[ ${HATCH_HOOK_DRY_RUN:-0} == 1 ]]; then dry_args+=(--dry-run); fi

if ! result=$("$PYTHON" "$SOURCE/wake_hook.py" --config "$CONFIG" "${dry_args[@]}"); then
    log "wake hook failed" '{}'
    silent "wake hook failed" '{}'
    exit 0
fi
if ! fields=$("$PYTHON" -c '
import json, sys
value = json.load(sys.stdin)
assert value["action"] in ("wake", "silent")
assert isinstance(value["reason"], str) and not any(c in value["reason"] for c in "\r\n")
assert isinstance(value["payload"], dict)
print(value["action"])
print(value["reason"])
print(json.dumps(value["payload"], separators=(",", ":")))
print("true" if value["payload"].get("check_failed") else "false")
' <<<"$result"); then
    log "wake hook invalid result" '{}'
    silent "wake hook invalid result" '{}'
    exit 0
fi
mapfile -t fields <<<"$fields"
action=${fields[0]}
reason=${fields[1]}
payload=${fields[2]}
if [[ ${fields[3]} == true ]]; then
    log "$reason" "$payload"
fi
if [[ $action == wake ]]; then
    wake "$reason" "$payload"
else
    silent "$reason" "$payload"
fi
