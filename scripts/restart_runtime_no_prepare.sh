#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DEPLOYMENT_CONFIG="configs/deployment_topology.local_192.json"

usage() {
  cat <<'USAGE'
Usage: bash scripts/restart_runtime_no_prepare.sh [options]

不重新 prepare 的快速重启：
  1. down 停掉现有 scheduler/vehicles/depot 进程
  2. 清理 logs、.run/logs 和 result/message_capture
  3. 使用已有 result/configs 启动 scheduler、vehicles、depot 三组进程

Options:
  --deployment-config <path>   Deployment topology JSON
  -h, --help                   Show help

Backward compatible:
  bash scripts/restart_runtime_no_prepare.sh configs/deployment_topology.local_192.json
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment-config)
      DEPLOYMENT_CONFIG="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      if [[ "$1" == *.json && "$DEPLOYMENT_CONFIG" == "configs/deployment_topology.local_192.json" ]]; then
        DEPLOYMENT_CONFIG="$1"; shift
      else
        echo "Unknown arg: $1" >&2
        usage
        exit 1
      fi
      ;;
  esac
done

echo "[quick-restart] deployment=${DEPLOYMENT_CONFIG}"
echo "[quick-restart] stopping old scheduler/vehicles/depot"
bash scripts/run_three_platform_flow.sh down \
  --deployment-config "$DEPLOYMENT_CONFIG" >/dev/null 2>&1 || true

echo "[quick-restart] cleaning logs and result/message_capture, keeping generated configs/maps/results"
mkdir -p .run/logs .run/pids logs result/message_capture
find .run/logs -maxdepth 1 -type f -delete 2>/dev/null || true
find logs -maxdepth 1 -type f \( -name '*.log' -o -name '*.jsonl' \) -delete 2>/dev/null || true
find result/message_capture -type f -delete 2>/dev/null || true
find .run/pids -maxdepth 1 -type f -name '*.pid' -delete 2>/dev/null || true

echo "[quick-restart] starting scheduler from existing generated configs"
bash scripts/run_three_platform_flow.sh scheduler \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[quick-restart] starting vehicle gateway and vehicles from existing generated configs"
bash scripts/run_three_platform_flow.sh vehicles \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[quick-restart] starting depot gateway and depots from existing generated configs"
bash scripts/run_three_platform_flow.sh depot \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[quick-restart] done"
echo "[quick-restart] logs: .run/logs and logs/"
