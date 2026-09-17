#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PID_DIR=".run/pids"

SCHED_CFG=""
AUTO_DIR=""
MANUAL_VEHICLES=()

usage() {
  cat <<USAGE
Usage: scripts/stack_down.sh [options]

Options:
  --scheduler-config <path>       Scheduler config for port-based cleanup
  --vehicle-config <path>         Add one vehicle config (repeatable)
  --vehicles-dir <dir>            Auto-load all *.json in dir
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

list_pids_on_port() {
  local p="$1"
  ss -ltnp "( sport = :$p )" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p'
  ss -lunp "( sport = :$p )" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p'
}

stop_pid() {
  local pid="$1"
  local label="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "Stopped $label pid=$pid"
      return 0
    fi
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "Force stopped $label pid=$pid"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scheduler-config)
      SCHED_CFG="$2"; shift 2;;
    --vehicle-config)
      MANUAL_VEHICLES+=("$2"); shift 2;;
    --vehicles-dir)
      AUTO_DIR="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

VEHICLE_CFGS=()
if [[ ${#MANUAL_VEHICLES[@]} -gt 0 ]]; then
  VEHICLE_CFGS=("${MANUAL_VEHICLES[@]}")
elif [[ -n "$AUTO_DIR" ]]; then
  mapfile -t VEHICLE_CFGS < <(ls "$AUTO_DIR"/*.json 2>/dev/null | sort)
fi

declare -A SEEN_PIDS=()

if [[ ! -d "$PID_DIR" ]]; then
  mkdir -p "$PID_DIR"
fi

for pf in "$PID_DIR"/*.pid; do
  [[ -f "$pf" ]] || continue
  pid="$(cat "$pf")"
  name="$(basename "$pf" .pid)"
  if [[ -n "$pid" && -z "${SEEN_PIDS[$pid]:-}" ]]; then
    stop_pid "$pid" "$name"
    SEEN_PIDS["$pid"]=1
  fi
  rm -f "$pf"
done

PORTS=()
if [[ -n "$SCHED_CFG" && -f "$SCHED_CFG" ]]; then
  sched_port="$(json_get "$SCHED_CFG" "listen_port")"
  dash_port="$(json_get "$SCHED_CFG" "dashboard_port")"
  [[ -n "$sched_port" ]] && PORTS+=("$sched_port")
  [[ -n "$dash_port" ]] && PORTS+=("$dash_port")
fi

for cfg in "${VEHICLE_CFGS[@]}"; do
  [[ -f "$cfg" ]] || continue
  v_port="$(json_get "$cfg" "listen_port")"
  [[ -n "$v_port" ]] && PORTS+=("$v_port")
done

if [[ ${#PORTS[@]} -gt 0 ]]; then
  mapfile -t UNIQUE_PORTS < <(printf '%s\n' "${PORTS[@]}" | awk '!seen[$0]++')
  for p in "${UNIQUE_PORTS[@]}"; do
    mapfile -t PORT_PIDS < <(list_pids_on_port "$p" | awk '!seen[$0]++')
    for pid in "${PORT_PIDS[@]}"; do
      [[ -n "$pid" ]] || continue
      if [[ -n "${SEEN_PIDS[$pid]:-}" ]]; then
        continue
      fi
      stop_pid "$pid" "listener:$p"
      SEEN_PIDS["$pid"]=1
    done
  done
fi
