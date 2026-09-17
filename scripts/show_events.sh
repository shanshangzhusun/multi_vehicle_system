#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

TARGET="${1:-scheduler}"
KEYWORD="${2:-}"

if [[ "$TARGET" == "scheduler" ]]; then
  FILE="logs/scheduler_events.jsonl"
else
  FILE="logs/${TARGET}_events.jsonl"
fi

if [[ ! -f "$FILE" ]]; then
  echo "log file not found: $FILE"
  exit 1
fi

if [[ -n "$KEYWORD" ]]; then
  rg -n "$KEYWORD" "$FILE" | tail -n 80
else
  tail -n 80 "$FILE"
fi
