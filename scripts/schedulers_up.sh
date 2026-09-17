#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PLATFORMS_CONFIG="configs/scheduler_platforms.json"
MANIFEST=""

usage() {
  cat <<'USAGE'
Usage: scripts/schedulers_up.sh [--platforms-config path] [--manifest path]

Generate and start scheduler platform instances from a scheduler platforms JSON.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platforms-config)
      PLATFORMS_CONFIG="$2"; shift 2;;
    --manifest)
      MANIFEST="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

python3 scripts/gen_scheduler_platform_configs.py --platforms-config "$PLATFORMS_CONFIG"
if [[ -z "$MANIFEST" ]]; then
  MANIFEST="$(python3 - "$PLATFORMS_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(cfg.get("manifest", "result/configs/schedulers_manifest.json"))
PY
)"
fi

mkdir -p .run/logs .run/pids

python3 - "$MANIFEST" <<'PY' | while IFS='|' read -r node_id cfg_path port; do
import json, sys
manifest = json.load(open(sys.argv[1], "r", encoding="utf-8"))
for row in manifest.get("schedulers", []):
    print(f"{row['node_id']}|{row['config']}|{row['port']}")
PY
  pid_file=".run/pids/${node_id}.pid"
  log_file=".run/logs/${node_id}.log"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "Scheduler $node_id already running pid=$(cat "$pid_file")"
    continue
  fi
  if ss -ltnH "( sport = :$port )" | grep -q .; then
    echo "Scheduler $node_id port $port is already in use; skip start." >&2
    continue
  fi
  nohup python3 -u -m mvs.scheduler.scheduler_app --config "$cfg_path" > "$log_file" 2>&1 &
  echo $! > "$pid_file"
  sleep 0.8
  if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "Scheduler $node_id failed to stay running; log=$log_file" >&2
    tail -40 "$log_file" >&2 || true
    exit 1
  fi
  echo "Started scheduler $node_id pid=$(cat "$pid_file") config=$cfg_path"
done
