#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="configs/shp_four_wave_demo.json"
RUN_ROOT=""
SCHED_CFG=""
TRANSPORT="tcp"
GAP_SCALE="auto"
USE_VIRTUAL_DISPATCH=0
TARGET_HOST=""
TARGET_PORT=""

usage() {
  cat <<'USAGE'
Usage: scripts/upstream_run_wave_schedule.sh [options]

Send prepared wave task files one wave at a time, in upstream order.

Options:
  --config <path>             Scenario config path (default: configs/shp_four_wave_demo.json)
  --run-root <dir>            Generated runtime root; defaults to config out_root
  --scheduler-config <path>   Scheduler config; defaults to <run-root>/configs/scheduler_debug.json
  --transport <name>          Transport name (default: tcp)
  --target-host <host>        Override task receiver host
  --target-port <port>        Override task receiver port
  --gap-scale <factor|auto>   Scale real wait between waves (default: auto = config vehicle_tuning.realtime_scale)
  --virtual-dispatch          Keep virtual dispatch offsets while still sending wave-by-wave
USAGE
}

json_get() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
path, expr = sys.argv[1], sys.argv[2]
obj = json.load(open(path, "r", encoding="utf-8"))
cur = obj
for part in expr.split('.'):
    cur = cur[part]
print(cur)
PY
}

json_try_get() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
path, expr = sys.argv[1], sys.argv[2]
obj = json.load(open(path, "r", encoding="utf-8"))
cur = obj
try:
    for part in expr.split('.'):
        cur = cur[part]
except Exception:
    print("")
    raise SystemExit(0)
print(cur)
PY
}

sleep_scaled() {
  local gap="$1"
  local scale="$2"
  python3 - "$gap" "$scale" <<'PY'
import sys, time
gap = float(sys.argv[1])
scale = float(sys.argv[2])
sleep_sec = max(0.0, gap * scale)
if sleep_sec > 0:
    time.sleep(sleep_sec)
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"; shift 2;;
    --run-root)
      RUN_ROOT="$2"; shift 2;;
    --scheduler-config)
      SCHED_CFG="$2"; shift 2;;
    --transport)
      TRANSPORT="$2"; shift 2;;
    --target-host)
      TARGET_HOST="$2"; shift 2;;
    --target-port)
      TARGET_PORT="$2"; shift 2;;
    --gap-scale)
      GAP_SCALE="$2"; shift 2;;
    --virtual-dispatch)
      USE_VIRTUAL_DISPATCH=1; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

if [[ -z "$RUN_ROOT" ]]; then
  RUN_ROOT="$(json_get "$CONFIG_PATH" "out_root")"
fi

if [[ "$GAP_SCALE" == "auto" ]]; then
  GAP_SCALE="$(json_try_get "$CONFIG_PATH" "simulation.upstream_gap_scale")"
  if [[ -z "$GAP_SCALE" ]]; then
    GAP_SCALE="$(json_try_get "$CONFIG_PATH" "vehicle_tuning.realtime_scale")"
  fi
  [[ -z "$GAP_SCALE" ]] && GAP_SCALE="1.0"
fi

if [[ -z "$SCHED_CFG" ]]; then
  SCHED_CFG="$RUN_ROOT/configs/scheduler_debug.json"
fi

SCHED_HOST="$(json_try_get "$SCHED_CFG" "listen_host")"
SCHED_PORT="$(json_try_get "$SCHED_CFG" "listen_port")"
if [[ "$SCHED_HOST" == "0.0.0.0" || -z "$SCHED_HOST" ]]; then
  SCHED_HOST="127.0.0.1"
fi
if [[ -n "$TARGET_HOST" ]]; then
  SCHED_HOST="$TARGET_HOST"
fi
if [[ -n "$TARGET_PORT" ]]; then
  SCHED_PORT="$TARGET_PORT"
fi

mapfile -t WAVES < <(python3 - "$CONFIG_PATH" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
waves = cfg.get("waves", [])
for idx, wave in enumerate(waves, start=1):
    task_id = wave.get("task_id", f"task_wave_{idx:02d}")
    count = int(wave["count"])
    gap = wave.get("gap_after_send_sec", "")
    virtual_offset = sum(float(waves[j].get("gap_after_send_sec", 0) or 0) for j in range(idx - 1))
    print(f"{task_id}|{count}|{gap}|{virtual_offset}")
PY
)

if [[ ${#WAVES[@]} -eq 0 ]]; then
  echo "No waves found in config: $CONFIG_PATH" >&2
  exit 1
fi

VIRTUAL_BASE_ISO=""
if [[ "$USE_VIRTUAL_DISPATCH" == "1" ]]; then
  VIRTUAL_BASE_ISO="$(python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
PY
)"
fi

for idx in "${!WAVES[@]}"; do
  wave_num=$((idx + 1))
  IFS='|' read -r task_id count gap virtual_offset <<<"${WAVES[$idx]}"
  printf -v wave_tag "%02d" "$wave_num"
  task_file="$RUN_ROOT/tasks/task_wave_${wave_tag}.json"
  if [[ ! -f "$task_file" ]]; then
    echo "Task file not found: $task_file" >&2
    echo "Run: bash scripts/run_three_platform_flow.sh prepare --config $CONFIG_PATH" >&2
    exit 1
  fi

  send_args=(
    python3 scripts/send_task.py
    --transport "$TRANSPORT"
    --task "$task_file"
    --host "$SCHED_HOST"
    --port "$SCHED_PORT"
  )

  if [[ -n "$VIRTUAL_BASE_ISO" ]]; then
    dispatch_time="$(python3 - "$VIRTUAL_BASE_ISO" "$virtual_offset" <<'PY'
from datetime import timedelta
import sys
from mvs.common.models import parse_iso_time
base = parse_iso_time(sys.argv[1])
offset = float(sys.argv[2] or 0)
print((base + timedelta(seconds=offset)).isoformat())
PY
)"
    send_args+=(--dispatch-time "$dispatch_time")
  fi

  echo "[upstream] sending wave=${wave_num} task_id=${task_id} launches=${count}"
  "${send_args[@]}"

  if [[ "$idx" -lt $((${#WAVES[@]} - 1)) ]]; then
    gap="${gap:-0}"
    echo "[upstream] waiting $(python3 - "$gap" "$GAP_SCALE" <<'PY'
import sys
print(f"{max(0.0, float(sys.argv[1] or 0) * float(sys.argv[2])):.3f}")
PY
)s before next wave"
    sleep_scaled "$gap" "$GAP_SCALE"
  fi
done
