#!/usr/bin/env bash
set -euo pipefail

TARGET_HOST="${1:-192.168.2.8}"
TARGET_PORT="${2:-9191}"
MSG_IP="${3:-192.168.2.8}"
START_PORT="${4:-8601}"
END_PORT="${5:-8624}"
INTERVAL_SEC="${6:-0.1}"

TMP_JSON="/tmp/request_depot_score_single.json"

for p in $(seq "${START_PORT}" "${END_PORT}"); do
  printf '{"msg_type":"REQUEST_DEPOT_SCORE","msg_ip":"%s","port":%s}\n' \
    "${MSG_IP}" "${p}" > "${TMP_JSON}"

  python3 scripts/send_raw_json.py \
    --json "${TMP_JSON}" \
    --host "${TARGET_HOST}" \
    --port "${TARGET_PORT}" \
    --timeout 2.0

  sleep "${INTERVAL_SEC}"
done
