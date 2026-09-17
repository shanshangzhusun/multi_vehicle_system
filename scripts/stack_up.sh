#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

RUN_DIR=".run"
LOG_DIR="$RUN_DIR/logs"
PID_DIR="$RUN_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

SCHED_CFG="configs/scheduler.json"
VEHICLE_CFGS=("configs/vehicle_001.json" "configs/vehicle_002.json" "configs/vehicle_003.json")
VEHICLE_GATEWAY_CFG=""
SEND_DEMO=0
NO_SCHEDULER=0
DEMO_COUNT=5
DEMO_START_AFTER=40
DEMO_INTERVAL=15

usage() {
  cat <<USAGE
Usage: scripts/stack_up.sh [options]

Options:
  --scheduler-config <path>       Scheduler config (default: configs/scheduler.json)
  --vehicle-config <path>         Add one vehicle config (repeatable)
  --vehicles-dir <dir>            Auto-load all *.json in dir
  --vehicle-gateway-config <path> Vehicle gateway config
  --no-scheduler                  Start vehicles only; do not start scheduler
  --send-demo                     Auto generate+send a demo task
  --demo-count <n>                Demo launch count (default: 5)
  --demo-start-after <sec>        Demo first fire delay (default: 40)
  --demo-interval <sec>           Demo fire interval (default: 15)
  -h, --help                      Show this help
USAGE
}

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
  ss -ltnH "( sport = :$p )" | grep -q . && return 0
  ss -lunH "( sport = :$p )" | grep -q .
}

AUTO_DIR=""
MANUAL_VEHICLES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scheduler-config)
      SCHED_CFG="$2"; shift 2;;
    --vehicle-config)
      MANUAL_VEHICLES+=("$2"); shift 2;;
    --vehicles-dir)
      AUTO_DIR="$2"; shift 2;;
    --vehicle-gateway-config)
      VEHICLE_GATEWAY_CFG="$2"; shift 2;;
    --no-scheduler)
      NO_SCHEDULER=1; shift;;
    --send-demo)
      SEND_DEMO=1; shift;;
    --demo-count)
      DEMO_COUNT="$2"; shift 2;;
    --demo-start-after)
      DEMO_START_AFTER="$2"; shift 2;;
    --demo-interval)
      DEMO_INTERVAL="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

if [[ ${#MANUAL_VEHICLES[@]} -gt 0 ]]; then
  VEHICLE_CFGS=("${MANUAL_VEHICLES[@]}")
fi

if [[ -n "$AUTO_DIR" ]]; then
  mapfile -t VEHICLE_CFGS < <(ls "$AUTO_DIR"/*.json 2>/dev/null | sort)
fi

if [[ ${#VEHICLE_CFGS[@]} -eq 0 ]]; then
  echo "No vehicle configs found." >&2
  exit 1
fi

if [[ "$NO_SCHEDULER" != "1" ]]; then
  if [[ -f "$PID_DIR/scheduler.pid" ]] && kill -0 "$(cat "$PID_DIR/scheduler.pid")" 2>/dev/null; then
    echo "Scheduler already running with pid $(cat "$PID_DIR/scheduler.pid")"
  else
    sched_port="$(json_get "$SCHED_CFG" "listen_port")"
    if [[ -n "$sched_port" ]] && port_busy "$sched_port"; then
      echo "Scheduler port $sched_port is already in use. Skip starting scheduler." >&2
    else
      nohup python3 -u -m mvs.scheduler.scheduler_app --config "$SCHED_CFG" > "$LOG_DIR/scheduler.log" 2>&1 &
      echo $! > "$PID_DIR/scheduler.pid"
      echo "Started scheduler pid=$(cat "$PID_DIR/scheduler.pid")"
    fi
  fi
fi

if [[ "$NO_SCHEDULER" != "1" ]]; then
  sleep 1
fi

if [[ -n "$VEHICLE_GATEWAY_CFG" && -f "$VEHICLE_GATEWAY_CFG" ]]; then
  gateway_pid="$PID_DIR/vehicle_gateway.pid"
  gateway_log="$LOG_DIR/vehicle_gateway.log"
  gateway_port="$(json_get "$VEHICLE_GATEWAY_CFG" "listen_port")"
  if [[ -f "$gateway_pid" ]] && kill -0 "$(cat "$gateway_pid")" 2>/dev/null; then
    echo "Vehicle gateway already running pid=$(cat "$gateway_pid")"
  elif [[ -n "$gateway_port" ]] && port_busy "$gateway_port"; then
    echo "Vehicle gateway port $gateway_port is in use. Skip start." >&2
  else
    nohup python3 -u -m mvs.vehicle.vehicle_gateway --config "$VEHICLE_GATEWAY_CFG" > "$gateway_log" 2>&1 &
    echo $! > "$gateway_pid"
    echo "Started vehicle gateway pid=$(cat "$gateway_pid")"
  fi
fi

idx=0
for cfg in "${VEHICLE_CFGS[@]}"; do
  idx=$((idx+1))
  base="$(basename "$cfg" .json)"
  pid_file="$PID_DIR/${base}.pid"
  log_file="$LOG_DIR/${base}.log"
  v_port="$(json_get "$cfg" "listen_port")"

  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "Vehicle $base already running pid=$(cat "$pid_file")"
    continue
  fi
  if [[ -n "$v_port" ]] && port_busy "$v_port"; then
    echo "Vehicle $base port $v_port is in use. Skip start." >&2
    continue
  fi

  nohup python3 -u -m mvs.vehicle.vehicle_app --config "$cfg" > "$log_file" 2>&1 &
  echo $! > "$pid_file"
  echo "Started vehicle $base pid=$(cat "$pid_file")"
done

if [[ "$SEND_DEMO" == "1" ]]; then
  DEMO_TASK="tasks/auto_demo_task.json"
  sched_host="$(json_get "$SCHED_CFG" "listen_host")"
  sched_port="$(json_get "$SCHED_CFG" "listen_port")"
  if [[ "$sched_host" == "0.0.0.0" || -z "$sched_host" ]]; then
    sched_host="127.0.0.1"
  fi
  python3 scripts/gen_task.py --out "$DEMO_TASK" --count "$DEMO_COUNT" --start-after-sec "$DEMO_START_AFTER" --interval-sec "$DEMO_INTERVAL"
  python3 scripts/send_task.py --task "$DEMO_TASK" --host "$sched_host" --port "$sched_port"
fi

echo
dash_host="$(json_get "$SCHED_CFG" "dashboard_host")"
dash_port="$(json_get "$SCHED_CFG" "dashboard_port")"
if [[ "$dash_host" == "0.0.0.0" || -z "$dash_host" ]]; then
  dash_host="127.0.0.1"
fi
echo "Dashboard: http://$dash_host:$dash_port"
echo "Logs: $LOG_DIR"
echo "Status: scripts/stack_status.sh"
echo "Stop: scripts/stack_down.sh"
