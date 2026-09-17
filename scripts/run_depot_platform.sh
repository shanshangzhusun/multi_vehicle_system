#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

SCHEDULER_CONFIG="result/configs/scheduler_debug.json"
OUT_CONFIG="result/configs/depot_platform.json"
LISTEN_HOST="0.0.0.0"
LISTEN_PORT="9130"
ENABLE_DEPOT_MODEL=0
DEPOT_MODEL_HOST="127.0.0.1"
DEPOT_MODEL_PORT="9150"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scheduler-config)
      SCHEDULER_CONFIG="$2"; shift 2;;
    --out)
      OUT_CONFIG="$2"; shift 2;;
    --listen-host)
      LISTEN_HOST="$2"; shift 2;;
    --listen-port)
      LISTEN_PORT="$2"; shift 2;;
    -h|--help)
      echo "Usage: scripts/run_depot_platform.sh [--scheduler-config path] [--out path] [--listen-host host] [--listen-port port] [--enable-depot-model]"
      exit 0;;
    --enable-depot-model)
      ENABLE_DEPOT_MODEL=1; shift;;
    --depot-model-host)
      DEPOT_MODEL_HOST="$2"; shift 2;;
    --depot-model-port)
      DEPOT_MODEL_PORT="$2"; shift 2;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1;;
  esac
done

python3 - "$SCHEDULER_CONFIG" "$OUT_CONFIG" "$LISTEN_HOST" "$LISTEN_PORT" "$ENABLE_DEPOT_MODEL" "$DEPOT_MODEL_HOST" "$DEPOT_MODEL_PORT" <<'PY'
import json
import sys
from pathlib import Path

scheduler_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
listen_host = sys.argv[3]
listen_port = int(sys.argv[4])
enable_depot_model = bool(int(sys.argv[5]))
depot_model_host = sys.argv[6]
depot_model_port = int(sys.argv[7])
scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))

cfg = {
    "node_id": "depot_platform",
    "listen_host": listen_host,
    "listen_port": listen_port,
    "transport": scheduler.get("transport", {"type": "tcp"}),
    "map": scheduler["map"],
    "lane_graph_cache_limit": scheduler.get("lane_graph_cache_limit", 32768),
    "depot_capacity": scheduler.get("depot_capacity", 16),
    "reload_duration_sec": scheduler.get("reload_duration_sec", 60.0),
    "depot_model": {
        "enabled": enable_depot_model,
        "host": depot_model_host,
        "port": depot_model_port,
        "target": "depot_model",
        "initial_delay_sec": 0.5,
        "request_interval_sec": 8.0 if enable_depot_model else 0.0,
        "require_ack": True,
        "response_require_ack": False,
    },
    "event_log_path": "logs/depot_events.jsonl",
    "message_capture": {
        "enabled": True,
        "dir": "result/message_capture/depot/depot_platform",
    },
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out_path}")
PY

exec python3 -m mvs.depot.depot_app --config "$OUT_CONFIG"
