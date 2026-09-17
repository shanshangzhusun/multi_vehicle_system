#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PID_DIR=".run/pids"
LOG_DIR=".run/logs"

if [[ ! -d "$PID_DIR" ]]; then
  echo "No running stack (no .run/pids)."
  exit 0
fi

echo "Process status:"
names=()
for pf in "$PID_DIR"/*.pid; do
  [[ -f "$pf" ]] || continue
  pid="$(cat "$pf")"
  name="$(basename "$pf" .pid)"
  names+=("$name")
  if kill -0 "$pid" 2>/dev/null; then
    echo "  [UP]   $name pid=$pid"
  else
    echo "  [DOWN] $name pid=$pid"
  fi
done

echo
echo "Recent logs:"
for name in "${names[@]}"; do
  lf="$LOG_DIR/${name}.log"
  [[ -f "$lf" ]] || continue
  echo "--- $(basename "$lf") ---"
  tail -n 5 "$lf" || true
done
