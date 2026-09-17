#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

TASK_PATH=""
SCHED_CFG="result/configs/scheduler_debug.json"
HOST=""
PORT=""
DISPATCH_TIME=""
TRANSPORT="tcp"

usage() {
  cat <<'USAGE'
Usage: scripts/upstream_task_send.sh --task <path> [options]

Send one task package as the upstream platform.

Options:
  --task <path>               Task JSON file to send
  --scheduler-config <path>   Scheduler config path (default: result/configs/scheduler_debug.json)
  --host <host>               Override scheduler host; defaults to scheduler config listen_host
  --port <port>               Override scheduler port; defaults to scheduler config listen_port
  --dispatch-time <iso>       Optional virtual dispatch time passed through to send_task.py
  --transport <name>          Transport name (default: tcp)
USAGE
}

json_get() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get(sys.argv[2], ""))
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)
      TASK_PATH="$2"; shift 2;;
    --scheduler-config)
      SCHED_CFG="$2"; shift 2;;
    --host)
      HOST="$2"; shift 2;;
    --port)
      PORT="$2"; shift 2;;
    --dispatch-time)
      DISPATCH_TIME="$2"; shift 2;;
    --transport)
      TRANSPORT="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

if [[ -z "$TASK_PATH" ]]; then
  echo "--task is required" >&2
  usage
  exit 1
fi

if [[ -z "$HOST" ]]; then
  HOST="$(json_get "$SCHED_CFG" "listen_host")"
  if [[ "$HOST" == "0.0.0.0" || -z "$HOST" ]]; then
    HOST="127.0.0.1"
  fi
fi

if [[ -z "$PORT" ]]; then
  PORT="$(json_get "$SCHED_CFG" "listen_port")"
fi

CMD=(
  python3 scripts/send_task.py
  --transport "$TRANSPORT"
  --task "$TASK_PATH"
  --host "$HOST"
  --port "$PORT"
)

if [[ -n "$DISPATCH_TIME" ]]; then
  CMD+=(--dispatch-time "$DISPATCH_TIME")
fi

echo "Sending upstream task to $HOST:$PORT"
exec "${CMD[@]}"
