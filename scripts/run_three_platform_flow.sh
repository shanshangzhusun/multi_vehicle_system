#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="configs/shp_four_wave_demo.json"
DEPLOYMENT_CONFIG=""
RUN_ROOT=""
SCHEDULER_PLATFORMS_CONFIG="configs/scheduler_platforms.json"
SIMULATION_MODE=""
VEHICLE_REALTIME_SCALE=""
EXPORT_WAIT_COMPLETION=1
EXPORT_COMPLETION_TIMEOUT="600"
EXPORT_COMPLETION_POLL_SEC="2"
ADVERTISE_HOST="${MVS_ADVERTISE_HOST:-}"
VEHICLE_COUNT_OVERRIDE=""

usage() {
  cat <<'USAGE'
Usage: scripts/run_three_platform_flow.sh <command> [options]

Commands:
  prepare               Stop old runtime, clear outputs, then generate map/runtime/config outputs
  scheduler             Start scheduler software for local debugging
  scheduler-server      Start scheduler software for LAN/server debugging
  vehicles              Start vehicle/fire-platform software for local debugging
  vehicles-server       Start vehicle/fire-platform software for LAN/server debugging
  depot                 Start depot gateway and all depot processes locally
  depot-server          Start depot gateway and all depot processes on LAN/server
  model                 Start one combined external model process for local debugging
  model-server          Start one combined external model process for LAN/server debugging
  fire-node             Start external fire-platform node for local debugging
  fire-node-server      Start external fire-platform node for LAN/server debugging
  depot-node            Start external depot node for local debugging (legacy/optional)
  depot-node-server     Start external depot node for LAN/server debugging (legacy/optional)
  export      Export current results as planning JSON and leader package
  reset       Stop local stack and clear logs/results for a clean new run
  status      Show current scheduler/vehicle stack status
  down        Stop current scheduler/vehicle stack

Shared options:
  --config <path>               Scenario config path (default: configs/shp_four_wave_demo.json)
  --deployment-config <path>    Deployment topology JSON: map, vehicles, schedulers, depots
  --run-root <dir>              Generated runtime root; defaults to config out_root
  --scheduler-platforms-config <path> Multi-scheduler platform config (default: configs/scheduler_platforms.json)
  --simulation-mode <mode>      Prepare command override (realtime/discrete_event)
  --vehicle-realtime-scale <n>  Prepare command override for vehicle execution scale
  --vehicle-count <n>           Override local vehicle/runtime count for prepare/start
  --wait-completion             Export command waits until mission subtasks settle (default)
  --no-wait-completion          Export current snapshot immediately
  --completion-timeout <sec>    Export wait timeout (default: 600)
  --completion-poll-sec <sec>   Export wait poll interval (default: 2)
  --advertise-host <ip>         IP announced to other machines, e.g. 192.168.2.8

Local split:
  Terminal 1: bash scripts/run_three_platform_flow.sh scheduler --deployment-config configs/deployment_topology.json
  Terminal 2: bash scripts/run_three_platform_flow.sh vehicles --deployment-config configs/deployment_topology.json
  Terminal 3: bash scripts/run_three_platform_flow.sh model --deployment-config configs/deployment_topology.json
  Terminal 4: bash scripts/run_three_platform_flow.sh fire-node --deployment-config configs/deployment_topology.json
  Current default flow is prelaunch-only: one model, one fire-node, one scheduler, vehicle software.
  The model process owns task injection and context serving. No separate upstream process is needed.

Server/LAN split:
  Add --advertise-host <server-ip>, or set MVS_ADVERTISE_HOST.
  Current default stack uses scheduler-server / vehicles-server / model-server / fire-node-server.
  Depot command starts one gateway plus the configured per-theater depot fleet.
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
try:
    obj = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    cur = obj
    for part in sys.argv[2].split('.'):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(part)
        cur = cur[part]
    print(cur)
except Exception:
    pass
PY
}

load_deployment_advertise_host() {
  if [[ -n "$ADVERTISE_HOST" || -z "$DEPLOYMENT_CONFIG" || ! -f "$DEPLOYMENT_CONFIG" ]]; then
    return
  fi
  if [[ "$COMMAND" != *-server ]]; then
    return
  fi
  ADVERTISE_HOST="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json
import sys

try:
    obj = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    obj = {}
network = obj.get("network") if isinstance(obj.get("network"), dict) else {}
host = str(network.get("advertise_host") or obj.get("advertise_host") or "").strip()
print(host)
PY
)"
  if [[ -n "$ADVERTISE_HOST" ]]; then
    export MVS_ADVERTISE_HOST="$ADVERTISE_HOST"
  fi
}

resolve_run_root() {
  if [[ -n "$RUN_ROOT" ]]; then
    return
  fi
  if [[ -n "$DEPLOYMENT_CONFIG" ]]; then
    RUN_ROOT="$(python3 - "$DEPLOYMENT_CONFIG" "$CONFIG_PATH" <<'PY'
import json, sys
deployment = json.load(open(sys.argv[1], "r", encoding="utf-8"))
fallback = json.load(open(sys.argv[2], "r", encoding="utf-8"))
print((deployment.get("runtime") or {}).get("out_root") or fallback.get("out_root", "result"))
PY
)"
  else
    RUN_ROOT="$(json_get "$CONFIG_PATH" "out_root")"
  fi
}

prune_task_files_for_wave_mode() {
  local tasks_dir="$1"
  if [[ ! -d "$tasks_dir" ]]; then
    return
  fi
  shopt -s nullglob
  local wave_tasks=("$tasks_dir"/task_wave_*.json)
  local non_wave_tasks=()
  local path=""
  for path in "$tasks_dir"/task_*.json; do
    if [[ "$(basename "$path")" != task_wave_* ]]; then
      non_wave_tasks+=("$path")
    fi
  done
  if (( ${#wave_tasks[@]} > 0 && ${#non_wave_tasks[@]} > 0 )); then
    rm -f "${non_wave_tasks[@]}"
    echo "[prepare] removed non-wave task files; current flow keeps only task_wave_*"
  fi
  shopt -u nullglob
}

auto_advertise_host() {
  if [[ -n "$ADVERTISE_HOST" ]]; then
    return
  fi
  ADVERTISE_HOST="$(python3 - <<'PY'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    host = s.getsockname()[0]
    s.close()
except Exception:
    host = ""
if not host or host.startswith("127."):
    try:
        host = next(ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127."))
    except Exception:
        host = ""
print(host)
PY
)"
  if [[ -z "$ADVERTISE_HOST" ]]; then
    echo "Cannot auto-detect advertise host. Use --advertise-host <server-ip>." >&2
    exit 1
  fi
  export MVS_ADVERTISE_HOST="$ADVERTISE_HOST"
}

generate_lan_config() {
  local mode="${1:-local}"
  resolve_run_root
  if [[ "$mode" == "server" ]]; then
    auto_advertise_host
  fi
  if [[ -f "$RUN_ROOT/configs/scheduler_platforms.json" ]]; then
    SCHEDULER_PLATFORMS_CONFIG="$RUN_ROOT/configs/scheduler_platforms.json"
  fi
  mkdir -p "$RUN_ROOT/lan"
  SCHEDULER_MANIFEST="$(python3 - "$SCHEDULER_PLATFORMS_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(cfg.get("manifest", "result/configs/schedulers_manifest.json"))
PY
)"
  DEPLOYMENT_ARG="${DEPLOYMENT_CONFIG:-}"
  python3 - "$RUN_ROOT" "$RUN_ROOT/lan/config.generated.json" "$SCHEDULER_MANIFEST" "$DEPLOYMENT_ARG" "$ADVERTISE_HOST" "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

run_root = Path(sys.argv[1])
out_path = Path(sys.argv[2])
manifest_path = Path(sys.argv[3])
deployment_path = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
advertise_host = sys.argv[5] if len(sys.argv) > 5 else ""
scenario_path = Path(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None
cfg = json.loads(Path("external_lan/config.example.json").read_text(encoding="utf-8"))
deployment = json.loads(deployment_path.read_text(encoding="utf-8")) if deployment_path and deployment_path.exists() else {}
scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path and scenario_path.exists() else {}
network = deployment.get("network") if isinstance(deployment.get("network"), dict) else {}
local_host = str(network.get("local_host") or "127.0.0.1").strip()
model = deployment.get("model") if isinstance(deployment.get("model"), dict) else {}


def select_connect_host(row: dict, default: str = "127.0.0.1") -> str:
    host = str(row.get("host") or row.get("server_host") or "").strip()
    local = str(row.get("local_host") or network.get("local_host") or "").strip()
    if advertise_host:
        return str(advertise_host or host or network.get("advertise_host") or default).strip()
    if host and local in {"", "127.0.0.1", "0.0.0.0", "localhost"}:
        return host
    return local or host or default


def select_listen_host(row: dict, default: str = "0.0.0.0") -> str:
    if advertise_host:
        return str(row.get("listen_host") or "0.0.0.0").strip()
    return str(row.get("listen_host") or default).strip()

manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"schedulers": []}
schedulers = []
for row in manifest.get("schedulers", []):
    schedulers.append(
        {
            "node_id": row.get("node_id"),
            "host": row.get("host", "127.0.0.1"),
            "port": int(row["port"]),
            "dashboard_port": int(row.get("dashboard_port", 0) or 0),
        }
    )
if not schedulers:
    scheduler_cfg = json.loads((run_root / "configs/scheduler_debug.json").read_text(encoding="utf-8"))
    schedulers.append(
        {
            "node_id": "scheduler",
            "host": local_host,
            "port": int(scheduler_cfg.get("listen_port", 9120)),
            "dashboard_port": int(scheduler_cfg.get("dashboard_port", 19120)),
        }
    )
cfg["vehicles_dir"] = str((run_root / "configs/vehicles").as_posix())
cfg["scheduler_software"]["host"] = schedulers[0]["host"]
cfg["scheduler_software"]["port"] = schedulers[0]["port"]
cfg["scheduler_softwares"] = schedulers
schedulers_cfg = deployment.get("schedulers") or {}
cfg["scheduler_model"]["scheduler_routing"] = schedulers_cfg.get("routing", cfg["scheduler_model"].get("scheduler_routing", "primary"))
task_paths = sorted((run_root / "tasks").glob("task_*.json"))
wave_task_paths = [p for p in task_paths if p.name.startswith("task_wave_")]
if wave_task_paths:
    task_paths = wave_task_paths
task_files = [str(p.as_posix()) for p in task_paths]
waves = scenario.get("waves") or []
gaps = [float(w.get("gap_after_send_sec", 0) or 0) for w in waves]
gap_scale = model.get("task_gap_scale")
if gap_scale is None:
    gap_scale = scenario.get("vehicle_tuning", {}).get("realtime_scale", 1.0)
cfg["scheduler_model"]["task_source"] = {
    "enabled": True,
    "task_files": task_files,
    "gaps_sec": gaps,
    "gap_scale": float(gap_scale or 1.0),
    "start_delay_sec": float(model.get("task_start_delay_sec", 3.0) or 3.0),
    "vehicle_ready_min_online": int(model.get("vehicle_ready_min_online", 0) or 0),
    "vehicle_ready_min_online_ratio": float(model.get("vehicle_ready_min_online_ratio", 0.0) or 0.0),
    "vehicle_ready_timeout_sec": float(model.get("vehicle_ready_timeout_sec", 0.0) or 0.0),
    "vehicle_ready_poll_sec": float(model.get("vehicle_ready_poll_sec", 0.5) or 0.5),
    "vehicle_ready_stable_rounds": int(model.get("vehicle_ready_stable_rounds", 1) or 1),
    "auto_stop_on_completion": bool(model.get("auto_stop_on_completion", True)),
    "completion_poll_sec": float(model.get("completion_poll_sec", 2.0) or 2.0),
    "completion_stable_rounds": int(model.get("completion_stable_rounds", 3) or 3),
    "completion_timeout_sec": float(model.get("completion_timeout_sec", 7200.0) or 7200.0),
}
cfg["quiet_model_logs"] = bool(model.get("quiet_logs", True))
depot = deployment.get("depot") or {}
fire = deployment.get("fire_platform") or {}
if isinstance(model, dict) and model:
    unified_port = int(
        model.get("port")
        or model.get("unified_port")
        or model.get("scheduler_port")
        or cfg["scheduler_model"].get("port", 9160)
    )
    unified_host = select_connect_host(model, "127.0.0.1")
    cfg["scheduler_model"]["host"] = "0.0.0.0"
    cfg["scheduler_model"]["advertise_host"] = unified_host
    cfg["scheduler_model"]["port"] = unified_port
    cfg["scheduler_model"]["unified_model_enabled"] = True
    cfg["scheduler_model"]["prelaunch_only"] = bool(model.get("prelaunch_only", False))
    cfg["scheduler_model"]["external_vehicle_selection_enabled"] = bool(model.get("external_vehicle_selection_enabled", False))
    cfg["scheduler_model"]["require_nodes_for_dispatch"] = bool(model.get("require_nodes_for_dispatch", True))
    cfg["scheduler_model"]["required_node_timeout_sec"] = float(model.get("required_node_timeout_sec", 30.0) or 30.0)
    cfg["scheduler_model"]["required_node_poll_sec"] = float(model.get("required_node_poll_sec", 0.5) or 0.5)
    cfg["fire_platform_model"]["host"] = "0.0.0.0"
    cfg["fire_platform_model"]["advertise_host"] = unified_host
    cfg["fire_platform_model"]["port"] = unified_port
    cfg["depot_model"]["host"] = "0.0.0.0"
    cfg["depot_model"]["advertise_host"] = unified_host
    cfg["depot_model"]["port"] = unified_port
if isinstance(depot.get("software"), dict):
    cfg["depot_software"]["host"] = select_connect_host(depot["software"], "127.0.0.1")
    cfg["depot_software"]["port"] = int(depot["software"].get("port", 9130))
else:
    cfg["depot_software"]["host"] = local_host
    cfg["depot_software"]["port"] = 9130
if (not model) and isinstance(depot.get("model"), dict):
    cfg["depot_model"]["host"] = select_listen_host(depot["model"], "0.0.0.0")
    cfg["depot_model"]["advertise_host"] = select_connect_host(depot["model"], "127.0.0.1")
    cfg["depot_model"]["port"] = int(depot["model"].get("port", 9150))
if (not model) and isinstance(fire.get("model"), dict):
    cfg["fire_platform_model"]["host"] = select_listen_host(fire["model"], "0.0.0.0")
    cfg["fire_platform_model"]["advertise_host"] = select_connect_host(fire["model"], "127.0.0.1")
    cfg["fire_platform_model"]["port"] = int(fire["model"].get("port", 9140))
if isinstance(fire.get("node"), dict):
    cfg["fire_platform_node"]["host"] = select_listen_host(fire["node"], "0.0.0.0")
    cfg["fire_platform_node"]["advertise_host"] = select_connect_host(fire["node"], "127.0.0.1")
    cfg["fire_platform_node"]["port"] = int(fire["node"].get("port", 9170))
    cfg["fire_platform_node"]["request_interval_sec"] = float(
        fire["node"].get("request_interval_sec", cfg["fire_platform_node"].get("request_interval_sec", 10.0)) or 10.0
    )
    cfg["fire_platform_node"]["external_vehicle_selection_enabled"] = bool(model.get("external_vehicle_selection_enabled", False)) if isinstance(model, dict) else False
    cfg["fire_platform_node"]["score_port_base"] = int(fire["node"].get("score_port_base", 8000) or 8000)
    cfg["fire_platform_node"]["score_port_count"] = int(fire["node"].get("score_port_count", 128) or 128)
    cfg["fire_platform_node"]["score_select_settle_sec"] = float(fire["node"].get("score_select_settle_sec", 1.0) or 1.0)
    cfg["fire_platform_node"]["selection_candidate_limit"] = int(
        fire["node"].get("selection_candidate_limit", cfg["fire_platform_node"].get("selection_candidate_limit", 32)) or 32
    )
if isinstance(depot.get("node"), dict):
    cfg["depot_node"]["host"] = select_listen_host(depot["node"], "0.0.0.0")
    cfg["depot_node"]["advertise_host"] = select_connect_host(depot["node"], "127.0.0.1")
    cfg["depot_node"]["port"] = int(depot["node"].get("port", 9180))
if advertise_host:
    for section in ("scheduler_model", "fire_platform_model", "fire_platform_node", "depot_model", "depot_node"):
        cfg[section]["host"] = "0.0.0.0"
        cfg[section]["advertise_host"] = advertise_host
out_path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
print(
    "wrote LAN config",
    out_path,
    "schedulers=",
    len(schedulers),
    "advertise_host=",
    advertise_host or "local",
)
PY
}

start_lan_role() {
  local role="$1"
  local mode="${2:-local}"
  generate_lan_config "$mode"
  mkdir -p .run/logs .run/pids
  local log_file=".run/logs/${role}.log"
  local pid_file=".run/pids/${role}.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$role already running pid=$(cat "$pid_file")"
    return 0
  fi
  : > "$log_file"
  local child_pid=""
  bash scripts/run_lan_mock_platforms.sh "$role" "$RUN_ROOT/lan/config.generated.json" >"$log_file" 2>&1 &
  child_pid=$!
  echo "$child_pid" > "$pid_file"
  cleanup_lan_role() {
    local status=$?
    trap - INT TERM EXIT
    if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
      kill "$child_pid" 2>/dev/null || true
      wait "$child_pid" 2>/dev/null || true
    fi
    [[ -n "${pid_file:-}" ]] && rm -f "$pid_file"
    exit "$status"
  }
  trap cleanup_lan_role INT TERM EXIT
  tail -n +1 -f --pid="$child_pid" "$log_file"
  wait "$child_pid"
  local status=$?
  trap - INT TERM EXIT
  [[ -n "${pid_file:-}" ]] && rm -f "$pid_file"
  return "$status"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

COMMAND="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"; shift 2;;
    --deployment-config)
      DEPLOYMENT_CONFIG="$2"; shift 2;;
    --run-root)
      RUN_ROOT="$2"; shift 2;;
    --scheduler-platforms-config)
      SCHEDULER_PLATFORMS_CONFIG="$2"; shift 2;;
    --simulation-mode)
      SIMULATION_MODE="$2"; shift 2;;
    --vehicle-realtime-scale)
      VEHICLE_REALTIME_SCALE="$2"; shift 2;;
    --vehicle-count)
      VEHICLE_COUNT_OVERRIDE="$2"; shift 2;;
    --wait-completion)
      EXPORT_WAIT_COMPLETION=1; shift;;
    --no-wait-completion)
      EXPORT_WAIT_COMPLETION=0; shift;;
    --completion-timeout)
      EXPORT_COMPLETION_TIMEOUT="$2"; shift 2;;
    --completion-poll-sec)
      EXPORT_COMPLETION_POLL_SEC="$2"; shift 2;;
    --advertise-host)
      ADVERTISE_HOST="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

if [[ -n "$ADVERTISE_HOST" ]]; then
  export MVS_ADVERTISE_HOST="$ADVERTISE_HOST"
fi
load_deployment_advertise_host

effective_deployment_config() {
  if [[ -z "$DEPLOYMENT_CONFIG" || -z "$VEHICLE_COUNT_OVERRIDE" ]]; then
    printf '%s\n' "$DEPLOYMENT_CONFIG"
    return
  fi
  resolve_run_root
  local out="$RUN_ROOT/configs/deployment.override.json"
  python3 - "$DEPLOYMENT_CONFIG" "$out" "$VEHICLE_COUNT_OVERRIDE" <<'PY'
import json, sys
src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = json.load(open(src, "r", encoding="utf-8"))
vehicles = cfg.get("vehicles") if isinstance(cfg.get("vehicles"), dict) else {}
primary_count = int(vehicles.get("vehicle_count", vehicles.get("count", 64)) or 64)
if count <= primary_count:
    vehicles["count"] = count
    vehicles["vehicle_count"] = count
    vehicles["enable_extended_128"] = False
    vehicles["extended_vehicle_count"] = 0
else:
    vehicles["count"] = primary_count
    vehicles["vehicle_count"] = primary_count
    vehicles["enable_extended_128"] = True
    vehicles["extended_vehicle_count"] = max(0, count - primary_count)
cfg["vehicles"] = vehicles
fire = cfg.get("fire_platform") if isinstance(cfg.get("fire_platform"), dict) else {}
node = fire.get("node") if isinstance(fire.get("node"), dict) else {}
if node:
    node["score_port_count"] = count
    node["selection_candidate_limit"] = count
    fire["node"] = node
    cfg["fire_platform"] = fire
with open(out, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=True, indent=2)
    f.write("\n")
print(out)
PY
}

case "$COMMAND" in
  prepare)
    resolve_run_root
    bash scripts/reset_mission_runtime.sh \
      --config "$CONFIG_PATH" \
      --run-root "$RUN_ROOT"
    EFFECTIVE_CONFIG="$CONFIG_PATH"
    EFFECTIVE_DEPLOYMENT_CONFIG="$(effective_deployment_config)"
    if [[ -n "$EFFECTIVE_DEPLOYMENT_CONFIG" ]]; then
      mkdir -p "$RUN_ROOT/configs"
      EFFECTIVE_CONFIG="$RUN_ROOT/configs/scenario.from_deployment.json"
      python3 scripts/apply_deployment_config.py \
        --stage scenario \
        --deployment-config "$EFFECTIVE_DEPLOYMENT_CONFIG" \
        --base-config "$CONFIG_PATH" \
        --run-root "$RUN_ROOT" \
        --out "$EFFECTIVE_CONFIG" >/dev/null
      echo "[prepare] using deployment topology: $EFFECTIVE_DEPLOYMENT_CONFIG"
    fi
    prepare_args=(
      bash scripts/run_mission_demo.sh
      --config "$EFFECTIVE_CONFIG" \
      --out-root "$RUN_ROOT" \
      --prepare-only
    )
    if [[ -n "$SIMULATION_MODE" ]]; then
      prepare_args+=(--simulation-mode "$SIMULATION_MODE")
    fi
    if [[ -n "$VEHICLE_REALTIME_SCALE" ]]; then
      prepare_args+=(--vehicle-realtime-scale "$VEHICLE_REALTIME_SCALE")
    fi
    "${prepare_args[@]}"
    prune_task_files_for_wave_mode "$RUN_ROOT/tasks"
    if [[ -n "$EFFECTIVE_DEPLOYMENT_CONFIG" ]]; then
      python3 scripts/apply_deployment_config.py \
        --stage runtime \
        --deployment-config "$EFFECTIVE_DEPLOYMENT_CONFIG" \
        --run-root "$RUN_ROOT"
      python3 scripts/generate_depot_configs.py \
        --deployment-config "$EFFECTIVE_DEPLOYMENT_CONFIG" \
        --run-root "$RUN_ROOT"
      SCHEDULER_PLATFORMS_CONFIG="$RUN_ROOT/configs/scheduler_platforms.json"
    fi
    ;;
  scheduler|scheduler-server)
    if [[ "$COMMAND" == "scheduler-server" ]]; then
      auto_advertise_host
    fi
    resolve_run_root
    if [[ -f "$RUN_ROOT/configs/scheduler_platforms.json" ]]; then
      SCHEDULER_PLATFORMS_CONFIG="$RUN_ROOT/configs/scheduler_platforms.json"
    fi
    python3 - "$SCHEDULER_PLATFORMS_CONFIG" "$RUN_ROOT/configs/scheduler_debug.json" "$ADVERTISE_HOST" <<'PY'
import json
import sys
from pathlib import Path

platforms_path = Path(sys.argv[1])
base_config = sys.argv[2]
advertise_host = sys.argv[3] if len(sys.argv) > 3 else ""
cfg = json.loads(platforms_path.read_text(encoding="utf-8"))
cfg["base_config"] = base_config
cfg.setdefault("out_dir", "result/configs/schedulers")
cfg.setdefault("manifest", "result/configs/schedulers_manifest.json")
if advertise_host:
    platforms = cfg.get("platforms") or []
    if not platforms:
        platforms = [{"host": advertise_host, "listen_host": "0.0.0.0"}]
    else:
        for row in platforms:
            row.setdefault("listen_host", "0.0.0.0")
            row["host"] = advertise_host
    cfg["platforms"] = platforms
platforms_path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
print("using scheduler platforms config", platforms_path)
PY
    bash scripts/schedulers_up.sh --platforms-config "$SCHEDULER_PLATFORMS_CONFIG"
    ;;
  depot|depot-server)
    if [[ "$COMMAND" == "depot-server" ]]; then
      auto_advertise_host
    fi
    resolve_run_root
    if [[ -z "$DEPLOYMENT_CONFIG" ]]; then
      echo "depot requires --deployment-config" >&2
      exit 1
    fi
    python3 scripts/generate_depot_configs.py \
      --deployment-config "$DEPLOYMENT_CONFIG" \
      --run-root "$RUN_ROOT"
    bash scripts/depots_up.sh --run-root "$RUN_ROOT"
    ;;
  vehicles-direct)
    resolve_run_root
    SCHED_HOST="127.0.0.1"
    SCHED_PORT="$(json_get "$RUN_ROOT/configs/scheduler_debug.json" "listen_port")"
    if [[ -f "$RUN_ROOT/configs/scheduler_platforms.json" ]]; then
      SCHEDULER_PLATFORMS_CONFIG="$RUN_ROOT/configs/scheduler_platforms.json"
    fi
    if [[ -f "$SCHEDULER_PLATFORMS_CONFIG" ]]; then
      SCHEDULER_MANIFEST="$(python3 - "$SCHEDULER_PLATFORMS_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(cfg.get("manifest", ""))
PY
)"
      if [[ -n "$SCHEDULER_MANIFEST" && -f "$SCHEDULER_MANIFEST" ]]; then
        read -r SCHED_HOST SCHED_PORT < <(python3 - "$SCHEDULER_MANIFEST" "$SCHED_HOST" "$SCHED_PORT" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], "r", encoding="utf-8"))
fallback_host, fallback_port = sys.argv[2], sys.argv[3]
rows = manifest.get("schedulers") or []
if rows:
    row = rows[0]
    print(row.get("host", fallback_host), int(row.get("port", fallback_port)))
else:
    print(fallback_host, fallback_port)
PY
)
      fi
    fi
    python3 - "$RUN_ROOT/configs/vehicles" "$SCHED_HOST" "$SCHED_PORT" <<'PY'
import json
import sys
from pathlib import Path

vehicles_dir = Path(sys.argv[1])
candidate_result_host = sys.argv[2]
candidate_result_port = int(sys.argv[3])
for path in sorted(vehicles_dir.glob("*.json")):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["candidate_result_host"] = candidate_result_host
    cfg["candidate_result_port"] = candidate_result_port
    cfg.pop("scheduler_host", None)
    cfg.pop("scheduler_port", None)
    path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"patched {path} -> candidate_result={candidate_result_host}:{candidate_result_port}")
PY
    bash scripts/stack_up.sh \
      --scheduler-config "$RUN_ROOT/configs/scheduler_debug.json" \
      --no-scheduler \
      --vehicles-dir "$RUN_ROOT/configs/vehicles"
    ;;
  vehicles|vehicles-server)
    if [[ "$COMMAND" == "vehicles-server" ]]; then
      auto_advertise_host
    fi
    resolve_run_root
    FIRE_MODEL_HOST="127.0.0.1"
    FIRE_MODEL_PORT="9160"
    if [[ -n "$DEPLOYMENT_CONFIG" ]]; then
      maybe_host="$(python3 - "$DEPLOYMENT_CONFIG" "$COMMAND" <<'PY'
import json
import sys

cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
command = sys.argv[2]
server_mode = command.endswith("-server")
network = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
row = model if model else (cfg.get("fire_platform", {}).get("model") if isinstance(cfg.get("fire_platform", {}), dict) else {})
if not isinstance(row, dict):
    row = {}
if server_mode:
    value = row.get("host") or row.get("server_host") or network.get("advertise_host") or ""
else:
    value = row.get("host") or row.get("advertise_host") or row.get("local_host") or network.get("advertise_host") or network.get("local_host") or "127.0.0.1"
print(str(value).strip())
PY
)"
      maybe_port="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
if isinstance(model, dict):
    if model.get("port") is not None:
        print(model.get("port"))
    elif model.get("unified_port") is not None:
        print(model.get("unified_port"))
    elif model.get("scheduler_port") is not None:
        print(model.get("scheduler_port"))
else:
    fire = cfg.get("fire_platform") if isinstance(cfg.get("fire_platform"), dict) else {}
    row = fire.get("model") if isinstance(fire.get("model"), dict) else {}
    if row.get("port") is not None:
        print(row.get("port"))
PY
)"
      [[ -n "$maybe_host" ]] && FIRE_MODEL_HOST="$maybe_host"
      [[ -n "$maybe_port" ]] && FIRE_MODEL_PORT="$maybe_port"
    fi
    if [[ -n "$ADVERTISE_HOST" ]]; then
      FIRE_MODEL_HOST="$ADVERTISE_HOST"
    fi
    EXTERNAL_SELECTION_ENABLED="$(json_try_get "$DEPLOYMENT_CONFIG" "model.external_vehicle_selection_enabled")"
    python3 - "$RUN_ROOT/configs/vehicles" "$FIRE_MODEL_HOST" "$FIRE_MODEL_PORT" "$EXTERNAL_SELECTION_ENABLED" <<'PY'
import json
import sys
from pathlib import Path

vehicles_dir = Path(sys.argv[1])
fire_model_host = sys.argv[2]
fire_model_port = int(sys.argv[3])
external_selection_enabled = str(sys.argv[4]).strip().lower() in {"1", "true", "yes", "on"}
for path in sorted(vehicles_dir.glob("*.json")):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["candidate_result_host"] = fire_model_host
    cfg["candidate_result_port"] = fire_model_port
    cfg.pop("scheduler_host", None)
    cfg.pop("scheduler_port", None)
    fire_model = dict(cfg.get("fire_platform_model", {}))
    fire_model.update(
        {
            "enabled": external_selection_enabled,
            "auto_request_enabled": False,
            "host": fire_model_host,
            "port": fire_model_port,
            "target": "scheduler_model",
            "request_interval_sec": 2.0,
        }
    )
    cfg["fire_platform_model"] = fire_model
    path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"patched {path} -> scheduler/fire_model={fire_model_host}:{fire_model_port}, fire_model={fire_model_host}:{fire_model_port}")
PY
    VEHICLE_GATEWAY_CFG="$RUN_ROOT/configs/vehicle_gateway.json"
    gateway_port="9190"
    if [[ -n "$DEPLOYMENT_CONFIG" ]]; then
      maybe_gateway_port="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
vehicles = cfg.get("vehicles") if isinstance(cfg.get("vehicles"), dict) else {}
print(vehicles.get("gateway_port", 9190))
PY
)"
      [[ -n "$maybe_gateway_port" ]] && gateway_port="$maybe_gateway_port"
    fi
    gateway_vehicle_host="192.168.2.8"
    gateway_node_host="192.168.2.12"
    if [[ -n "$DEPLOYMENT_CONFIG" ]]; then
      gateway_vehicle_host="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
network = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
vehicles = cfg.get("vehicles") if isinstance(cfg.get("vehicles"), dict) else {}
print(vehicles.get("host") or network.get("advertise_host") or network.get("local_host") or "192.168.2.8")
PY
)"
      gateway_node_host="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
fire = cfg.get("fire_platform") if isinstance(cfg.get("fire_platform"), dict) else {}
node = fire.get("node") if isinstance(fire.get("node"), dict) else {}
print(node.get("host") or node.get("local_host") or "192.168.2.12")
PY
)"
    fi
    manual_debug_enabled="true"
    if [[ -n "$DEPLOYMENT_CONFIG" ]]; then
      manual_debug_enabled="$(python3 - "$DEPLOYMENT_CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
row = cfg.get("manual_debug") if isinstance(cfg.get("manual_debug"), dict) else {}
print("true" if row.get("enabled", True) else "false")
PY
)"
    fi
    python3 - "$VEHICLE_GATEWAY_CFG" "$FIRE_MODEL_HOST" "$FIRE_MODEL_PORT" "$gateway_port" "$gateway_vehicle_host" "$gateway_node_host" "$RUN_ROOT/configs/vehicles" "$manual_debug_enabled" "$DEPLOYMENT_CONFIG" "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
path, host, port, gateway_port, vehicle_host, node_host, vehicles_dir, manual_debug_enabled, deployment_config, run_root = (
    sys.argv[1],
    sys.argv[2],
    int(sys.argv[3]),
    int(sys.argv[4]),
    sys.argv[5],
    sys.argv[6],
    Path(sys.argv[7]),
    sys.argv[8].strip().lower() in {"1", "true", "yes", "on"},
    sys.argv[9],
    Path(sys.argv[10]),
)
deployment = {}
if deployment_config:
    with open(deployment_config, "r", encoding="utf-8") as f:
        deployment = json.load(f)
capture_cfg = deployment.get("message_capture") if isinstance(deployment.get("message_capture"), dict) else {}
capture_enabled = bool(capture_cfg.get("enabled", False))
ports = []
for vehicle_path in sorted(vehicles_dir.glob("*.json")):
    vehicle_cfg = json.load(open(vehicle_path, "r", encoding="utf-8"))
    ports.append(int(vehicle_cfg.get("listen_port") or vehicle_cfg.get("advertise_port") or vehicle_cfg.get("vehicle_id")))
vehicle_count = len(ports)
cfg = {
    "node_id": "vehicle_gateway",
    "listen_host": "0.0.0.0",
    "listen_port": gateway_port,
    "model_host": host,
    "model_port": port,
    "vehicle_host": vehicle_host,
    "node_host": node_host,
    "vehicle_start_port": min(ports) if ports else 8414,
    "vehicle_count": vehicle_count,
    "vehicle_ports": ports,
    "event_log_path": "logs/vehicle_gateway_events.jsonl",
    "message_capture": {
        "enabled": capture_enabled,
        "dir": str((run_root / "message_capture" / "vehicle" / "gateway").as_posix()),
    },
    "manual_debug": {"enabled": manual_debug_enabled},
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=True, indent=2)
    f.write("\n")
print(f"wrote vehicle gateway config {path}")
PY
    bash scripts/stack_up.sh \
      --scheduler-config "$RUN_ROOT/configs/scheduler_debug.json" \
      --no-scheduler \
      --vehicle-gateway-config "$VEHICLE_GATEWAY_CFG" \
      --vehicles-dir "$RUN_ROOT/configs/vehicles"
    ;;
  lan)
    start_lan_role "all" "local"
    ;;
  model)
    start_lan_role "model" "local"
    ;;
  model-server)
    start_lan_role "model" "server"
    ;;
  fire-node)
    start_lan_role "fire_platform_node" "local"
    ;;
  fire-node-server)
    start_lan_role "fire_platform_node" "server"
    ;;
  depot-node)
    start_lan_role "depot_node" "local"
    ;;
  depot-node-server)
    start_lan_role "depot_node" "server"
    ;;
  export)
    resolve_run_root
    export_args=(
      bash scripts/export_mission_package.sh
      --run-root "$RUN_ROOT" \
      --logs-dir logs \
      --scheduler-config "$RUN_ROOT/configs/scheduler_debug.json"
    )
    if [[ "$EXPORT_WAIT_COMPLETION" == "1" ]]; then
      export_args+=(
        --wait-completion
        --completion-timeout "$EXPORT_COMPLETION_TIMEOUT"
        --completion-poll-sec "$EXPORT_COMPLETION_POLL_SEC"
      )
    fi
    "${export_args[@]}"
    ;;
  reset)
    resolve_run_root
    bash scripts/reset_mission_runtime.sh \
      --config "$CONFIG_PATH" \
      --run-root "$RUN_ROOT"
    ;;
  status)
    bash scripts/stack_status.sh
    ;;
  down)
    resolve_run_root
    bash scripts/stack_down.sh \
      --scheduler-config "$RUN_ROOT/configs/scheduler_debug.json" \
      --vehicles-dir "$RUN_ROOT/configs/vehicles"
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    usage
    exit 1
    ;;
esac
