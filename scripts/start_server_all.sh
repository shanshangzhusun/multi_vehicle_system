#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DEPLOYMENT_CONFIG="configs/deployment_topology.local_192.json"
OPEN_CODE=0

usage() {
  cat <<'USAGE'
Usage: bash scripts/start_server_all.sh [options]

开机后的一键启动：
  1. 激活 .venv（如果存在）
  2. prepare 清理旧运行态并重新生成配置
  3. 启动 scheduler、vehicles、depot 三组进程

Options:
  --deployment-config <path>   Deployment topology JSON
  --open-code                  同时打开 VS Code（默认不打开，避免服务器桌面卡顿）
  -h, --help                   Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deployment-config)
      DEPLOYMENT_CONFIG="$2"; shift 2 ;;
    --open-code)
      OPEN_CODE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [[ "$OPEN_CODE" == "1" ]]; then
  code . >/tmp/mvs_vscode.log 2>&1 &
fi

echo "[start-all] deployment=${DEPLOYMENT_CONFIG}"
echo "[start-all] prepare runtime and generated configs"
bash scripts/run_three_platform_flow.sh prepare \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[start-all] start scheduler"
bash scripts/run_three_platform_flow.sh scheduler \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[start-all] start vehicle gateway and vehicles"
bash scripts/run_three_platform_flow.sh vehicles \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[start-all] start depot gateway and depots"
bash scripts/run_three_platform_flow.sh depot \
  --deployment-config "$DEPLOYMENT_CONFIG"

echo "[start-all] multi_vehicle_system started"
echo "[start-all] logs: .run/logs and logs/"
