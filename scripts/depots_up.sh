#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT="result"
if [[ "${1:-}" == "--run-root" ]]; then
  RUN_ROOT="$2"
fi

DEPOTS_DIR="$RUN_ROOT/configs/depots"
GATEWAY_CONFIG="$RUN_ROOT/configs/depot_gateway.json"
mkdir -p .run/logs .run/pids

if [[ ! -f "$GATEWAY_CONFIG" ]]; then
  echo "Missing $GATEWAY_CONFIG. Run prepare first." >&2
  exit 1
fi

json_get() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
obj = json.load(open(path, "r", encoding="utf-8"))
print(obj.get(key, ""))
PY
}

port_busy() {
  local p="$1"
  ss -ltnH "( sport = :$p )" | grep -q .
}

start_process() {
  local name="$1"
  local port="$2"
  local pid_file="$3"
  local log_file="$4"
  shift 4

  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name already running pid=$(cat "$pid_file")"
    return 0
  fi
  if [[ -n "$port" ]] && port_busy "$port"; then
    echo "$name port $port is already in use; skip start." >&2
    return 0
  fi

  nohup "$@" > "$log_file" 2>&1 &
  echo $! > "$pid_file"
  sleep 0.2
  if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name failed to stay running; log=$log_file" >&2
    tail -40 "$log_file" >&2 || true
    exit 1
  fi
  echo "Started $name pid=$(cat "$pid_file")"
}

gateway_port="$(json_get "$GATEWAY_CONFIG" "listen_port")"
start_process \
  "depot gateway" \
  "$gateway_port" \
  ".run/pids/depot_gateway.pid" \
  ".run/logs/depot_gateway.log" \
  python3 -u -m mvs.depot.depot_gateway --config "$GATEWAY_CONFIG"

shopt -s nullglob
configs=("$DEPOTS_DIR"/*.json)
if (( ${#configs[@]} == 0 )); then
  echo "No depot configs found in $DEPOTS_DIR" >&2
  exit 1
fi
for config in "${configs[@]}"; do
  port="$(basename "$config" .json)"
  listen_port="$(json_get "$config" "listen_port")"
  if [[ -z "$listen_port" ]]; then
    listen_port="$port"
  fi
  start_process \
    "depot $port" \
    "$listen_port" \
    ".run/pids/depot_${port}.pid" \
    ".run/logs/depot_${port}.log" \
    python3 -u -m mvs.depot.depot_app --config "$config"
done

echo "Depot fleet ready: gateway + ${#configs[@]} depot processes"
echo "Logs: .run/logs/depot_gateway.log and .run/logs/depot_<port>.log"
echo "Stop/reset: bash scripts/run_three_platform_flow.sh down"
