#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

ROLE="${1:-all}"
CONFIG_PATH="${2:-external_lan/config.example.json}"

case "$ROLE" in
  model|scheduler_model|fire_platform_model|fire_platform_node|depot_model|depot_node|all)
    exec python3 external_lan/lan_platforms.py "$ROLE" --config "$CONFIG_PATH"
    ;;
  -h|--help)
    cat <<'USAGE'
Usage: scripts/run_lan_mock_platforms.sh <role> [config]

Roles:
  model                 Mock 统一模型：任务注入、调度上下文、发射上下文、贮备库上下文统一进程
  fire_platform_node    Mock 发射平台节点
  depot_node            Mock 贮备库节点
  all                   Start unified model plus both node mocks in one process
USAGE
    ;;
  *)
    echo "Unknown role: $ROLE" >&2
    exit 1
    ;;
esac
