#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 4 ]]; then
  echo "Usage: $0 <gateway_host> <gateway_port> <interval_sec> <port_or_range=msg_ip>..." >&2
  echo "Example: $0 192.168.2.8 9191 0.1 8601-8612=192.168.2.8 8613-8624=192.168.2.8" >&2
  exit 2
fi

TARGET_HOST="$1"
TARGET_PORT="$2"
INTERVAL_SEC="$3"
shift 3

TMP_JSON="/tmp/request_depot_score_single.json"

send_one() {
  local depot_port="$1"
  local msg_ip="$2"

  printf '{"msg_type":"REQUEST_DEPOT_SCORE","msg_ip":"%s","port":%s}\n' \
    "${msg_ip}" "${depot_port}" > "${TMP_JSON}"

  python3 scripts/send_raw_json.py \
    --json "${TMP_JSON}" \
    --host "${TARGET_HOST}" \
    --port "${TARGET_PORT}" \
    --timeout 2.0

  sleep "${INTERVAL_SEC}"
}

for spec in "$@"; do
  if [[ "${spec}" != *=* ]]; then
    echo "Invalid mapping '${spec}', expected port_or_range=msg_ip" >&2
    exit 2
  fi

  range="${spec%%=*}"
  msg_ip="${spec#*=}"

  if [[ "${range}" == *-* ]]; then
    start_port="${range%-*}"
    end_port="${range#*-}"
    for depot_port in $(seq "${start_port}" "${end_port}"); do
      send_one "${depot_port}" "${msg_ip}"
    done
  else
    send_one "${range}" "${msg_ip}"
  fi
done
