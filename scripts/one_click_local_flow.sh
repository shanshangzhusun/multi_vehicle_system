#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEPLOYMENT_CONFIG="configs/deployment_topology.local_16.json"
VEHICLE_COUNT=16
TASK_COUNT=16
REQUEST_COUNT=16
FIRE_TIME=1800
LISTEN_SEC=240
VEHICLE_START_PORT=8414
MODEL_HOST="127.0.0.10"
MODEL_PORT=8888
SCHEDULER_HOST="127.0.0.8"
SCHEDULER_PORT=9120
GATEWAY_HOST="127.0.0.8"
GATEWAY_PORT=9190
VEHICLE_HOST="127.0.0.8"
NODE_HOST="127.0.0.12"
FA_SHE_FILE="result/extracted_dian/FA_SHE_DIAN.json"
YIN_BI_FILE="result/extracted_dian/YIN_BI_DIAN.json"
DEPOT_FILE="result/extracted_dian/DEPOT_DIAN.json"
VEHICLE_FILE="result/extracted_dian/VEHICLE_DIAN.json"
AUTO_RENDER=1

usage() {
  cat <<'EOF'
Usage: bash scripts/one_click_local_flow.sh [options]

Options:
  --deployment-config <path>   Deployment config used by prepare
  --vehicle-count <n>          Number of vehicles to start and mock
  --task-count <n>             Number of tasks sent by mock
  --request-count <n>          Number of score requests sent by mock
  --fire-time <sec>            Fire time used by mock
  --listen-sec <sec>           Mock listen seconds
  --vehicle-start-port <port>  Starting vehicle port
  --fa-she-file <path>         Fire point json
  --yin-bi-file <path>         Hide point json
  --depot-file <path>          Depot point json
  --vehicle-file <path>        Vehicle point json
  --no-render                  Skip final route rendering
  -h, --help                   Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment-config) DEPLOYMENT_CONFIG="$2"; shift 2 ;;
    --vehicle-count) VEHICLE_COUNT="$2"; shift 2 ;;
    --task-count) TASK_COUNT="$2"; shift 2 ;;
    --request-count) REQUEST_COUNT="$2"; shift 2 ;;
    --fire-time) FIRE_TIME="$2"; shift 2 ;;
    --listen-sec) LISTEN_SEC="$2"; shift 2 ;;
    --vehicle-start-port) VEHICLE_START_PORT="$2"; shift 2 ;;
    --fa-she-file) FA_SHE_FILE="$2"; shift 2 ;;
    --yin-bi-file) YIN_BI_FILE="$2"; shift 2 ;;
    --depot-file) DEPOT_FILE="$2"; shift 2 ;;
    --vehicle-file) VEHICLE_FILE="$2"; shift 2 ;;
    --no-render) AUTO_RENDER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

echo "[one-click] stopping old stack"
bash scripts/stack_down.sh >/dev/null 2>&1 || true
rm -rf .run

echo "[one-click] preparing configs"
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config "$DEPLOYMENT_CONFIG" \
  --vehicle-count "$VEHICLE_COUNT"

SCHED_CFG="result/configs/scheduler_debug.json"
GATEWAY_CFG="result/configs/vehicle_gateway.local.json"
if [[ ! -f "$GATEWAY_CFG" ]]; then
  GATEWAY_CFG="result/configs/vehicle_gateway.json"
fi

mkdir -p .run/logs

echo "[one-click] starting scheduler"
nohup python3 -u -m mvs.scheduler.scheduler_app \
  --config "$SCHED_CFG" > .run/logs/one_click_scheduler.log 2>&1 &
SCHED_PID=$!
echo "[one-click] scheduler pid=$SCHED_PID"

sleep 2

echo "[one-click] starting gateway + vehicles"
bash scripts/stack_up.sh \
  --scheduler-config "$SCHED_CFG" \
  --no-scheduler \
  --vehicle-gateway-config "$GATEWAY_CFG" \
  --vehicles-dir result/configs/vehicles

echo "[one-click] starting mock all-in-one flow"
python3 scripts/mock_local_model_node.py all \
  --model-host "$MODEL_HOST" \
  --model-port "$MODEL_PORT" \
  --scheduler-host "$SCHEDULER_HOST" \
  --scheduler-port "$SCHEDULER_PORT" \
  --gateway-host "$GATEWAY_HOST" \
  --gateway-port "$GATEWAY_PORT" \
  --vehicle-host "$VEHICLE_HOST" \
  --node-host "$NODE_HOST" \
  --vehicle-start-port "$VEHICLE_START_PORT" \
  --vehicle-count "$VEHICLE_COUNT" \
  --request-count "$REQUEST_COUNT" \
  --task-count "$TASK_COUNT" \
  --fire-time "$FIRE_TIME" \
  --listen-sec "$LISTEN_SEC" \
  --send-task \
  --fa-she-file "$FA_SHE_FILE" \
  --yin-bi-file "$YIN_BI_FILE" \
  --depot-file "$DEPOT_FILE" \
  --vehicle-file "$VEHICLE_FILE"

if [[ "$AUTO_RENDER" == "1" && -f result/latest_dispatch_trajectory_bundle.json ]]; then
  echo "[one-click] rendering latest dispatch routes"
  python3 scripts/render_latest_dispatch_routes.py \
    --bundle result/latest_dispatch_trajectory_bundle.json \
    --scheduler-config "$SCHED_CFG" \
    --received-dir result/received_dian \
    --dian-dir result/extracted_dian \
    --out result/visuals/latest_dispatch_routes.png \
    --focus-routes-only || true
fi

echo "[one-click] done"
echo "[one-click] scheduler log: .run/logs/one_click_scheduler.log"
echo "[one-click] route image: result/visuals/latest_dispatch_routes.png"
