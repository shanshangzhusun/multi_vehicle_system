#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT="result"
CONFIG_PATH="configs/shp_four_wave_demo.json"

usage() {
  cat <<'USAGE'
Usage: scripts/reset_mission_runtime.sh [options]

Stop local scheduler/vehicles/depot/LAN model processes and clear runtime logs/results for a clean simulation run.

Options:
  --run-root <dir>   Runtime root (default: result)
  --config <path>    Scenario config for resolving generated runtime paths
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-root)
      RUN_ROOT="$2"; shift 2;;
    --config)
      CONFIG_PATH="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

bash scripts/run_three_platform_flow.sh down --config "$CONFIG_PATH" --run-root "$RUN_ROOT" >/dev/null 2>&1 || true

# The LAN demo can leave depot/model processes in separate terminals.
# Stop only the known local demo entrypoints so a fresh run cannot inherit old
# sockets, logs, or in-memory task state.
pkill -f "external_lan/lan_platforms.py" >/dev/null 2>&1 || true
pkill -f "mvs.depot.depot_app" >/dev/null 2>&1 || true
pkill -f "scripts/run_depot_platform.sh" >/dev/null 2>&1 || true

find logs -maxdepth 1 -type f -name '*.jsonl' -delete 2>/dev/null || true
find .run/logs -maxdepth 1 -type f -name '*.log' -delete 2>/dev/null || true
find .run/pids -maxdepth 1 -type f -name '*.pid' -delete 2>/dev/null || true

mkdir -p "$RUN_ROOT/visuals" "$RUN_ROOT/results/leader_package"
find "$RUN_ROOT/message_capture" -type f -delete 2>/dev/null || true
find "$RUN_ROOT/received_dian" -type f -delete 2>/dev/null || true
find "$RUN_ROOT/mock_inputs_from_received" -type f -delete 2>/dev/null || true
find "$RUN_ROOT/extracted_dian" -maxdepth 1 -type f -delete 2>/dev/null || true
find "$RUN_ROOT/lan" -maxdepth 1 -type f -name 'config.generated.json' -delete 2>/dev/null || true
find "$RUN_ROOT/configs/vehicles" -maxdepth 1 -type f -name '*.json' -delete 2>/dev/null || true
find "$RUN_ROOT/configs/schedulers" -maxdepth 1 -type f -name '*.json' -delete 2>/dev/null || true
find "$RUN_ROOT/configs" -maxdepth 1 -type f \( \
  -name 'scheduler_debug.json' -o \
  -name 'scheduler_platforms.json' -o \
  -name 'schedulers_manifest.json' -o \
  -name 'scenario.from_deployment.json' -o \
  -name 'deployment_applied.json' -o \
  -name 'deployment.override.json' -o \
  -name 'vehicle_gateway.json' -o \
  -name 'vehicle_gateway.local.json' -o \
  -name 'depot_platform.json' \
\) -delete 2>/dev/null || true
find "$RUN_ROOT" -maxdepth 1 -type f \( \
  -name 'latest_dispatch_trajectory_bundle*.json' -o \
  -name 'manual_selected_vehicle_result*.json' -o \
  -name 'candidate_launch_order*.json' -o \
  -name 'build_meta.json' -o \
  -name 'received_dian*.tar' -o \
  -name 'received_dian*.zip' \
\) -delete 2>/dev/null || true
find "$RUN_ROOT/visuals" -type f -delete 2>/dev/null || true
find "$RUN_ROOT" -maxdepth 1 -type d -name 'focus_*' -exec rm -rf {} + 2>/dev/null || true
find "$RUN_ROOT/tasks" -maxdepth 1 -type f -name 'task_*.json' -delete 2>/dev/null || true
find "$RUN_ROOT/visuals" -maxdepth 1 -type f \( \
  -name 'final_state.json' -o \
  -name 'mission_timeline.txt' -o \
  -name 'wave_timeline.txt' -o \
  -name 'wave_*_state.json' -o \
  -name 'wave_*_snapshot.png' -o \
  -name 'final_snapshot.png' \
\) -delete 2>/dev/null || true
find "$RUN_ROOT/results" -maxdepth 1 -type f \( \
  -name 'planning_results.json' -o \
  -name 'stage_timeline.json' -o \
  -name 'point_assignment.json' -o \
  -name 'realtime_pose.json' -o \
  -name 'standard_interface_manifest.json' -o \
  -name 'wave_dispatch_metrics.json' -o \
  -name 'action_audit.json' -o \
  -name 'lan_link_evidence.json' \
\) -delete 2>/dev/null || true
find "$RUN_ROOT/results/leader_package" -maxdepth 1 -type f -delete 2>/dev/null || true

echo "[reset] local scheduler/vehicles/depot/LAN model stopped"
echo "[reset] runtime logs and exported result files cleared"
