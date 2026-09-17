#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT="result"
LOGS_DIR="logs"
SCHED_CFG=""
DASH_HOST=""
DASH_PORT=""
CAPTURE_TIMEOUT="30"
WAIT_COMPLETION=0
COMPLETION_TIMEOUT="600"
COMPLETION_POLL_SEC="2"

usage() {
  cat <<'USAGE'
Usage: scripts/export_mission_package.sh [options]

Export the current three-platform simulation result in the same layout as the leader package.

Options:
  --run-root <dir>            Runtime root (default: result)
  --logs-dir <dir>            Logs directory (default: logs)
  --scheduler-config <path>   Scheduler config; defaults to <run-root>/configs/scheduler_debug.json
  --dashboard-host <host>     Dashboard host override
  --dashboard-port <port>     Dashboard port override
  --capture-timeout <sec>     Dashboard capture timeout (default: 30)
  --wait-completion           Wait until mission subtasks are no longer pending/running before export
  --completion-timeout <sec>  Max wait when --wait-completion is used (default: 600)
  --completion-poll-sec <sec> Poll interval when --wait-completion is used (default: 2)
USAGE
}

json_get() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get(sys.argv[2], ""))
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-root)
      RUN_ROOT="$2"; shift 2;;
    --logs-dir)
      LOGS_DIR="$2"; shift 2;;
    --scheduler-config)
      SCHED_CFG="$2"; shift 2;;
    --dashboard-host)
      DASH_HOST="$2"; shift 2;;
    --dashboard-port)
      DASH_PORT="$2"; shift 2;;
    --capture-timeout)
      CAPTURE_TIMEOUT="$2"; shift 2;;
    --wait-completion)
      WAIT_COMPLETION=1; shift;;
    --completion-timeout)
      COMPLETION_TIMEOUT="$2"; shift 2;;
    --completion-poll-sec)
      COMPLETION_POLL_SEC="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

if [[ -z "$SCHED_CFG" ]]; then
  SCHED_CFG="$RUN_ROOT/configs/scheduler_debug.json"
fi

if [[ -z "$DASH_HOST" ]]; then
  DASH_HOST="$(json_get "$SCHED_CFG" "dashboard_host")"
  if [[ "$DASH_HOST" == "0.0.0.0" || -z "$DASH_HOST" ]]; then
    DASH_HOST="127.0.0.1"
  fi
fi

if [[ -z "$DASH_PORT" ]]; then
  DASH_PORT="$(json_get "$SCHED_CFG" "dashboard_port")"
fi

mkdir -p "$RUN_ROOT/visuals" "$RUN_ROOT/results"

resolve_scheduler_log() {
  python3 - "$LOGS_DIR" <<'PY'
import json
import sys
from pathlib import Path

logs_dir = Path(sys.argv[1])
candidates = []
default = logs_dir / "scheduler_events.jsonl"
if default.exists():
    candidates.append(default)
candidates.extend(sorted(logs_dir.glob("scheduler_*_events.jsonl")))

def score(path: Path) -> tuple:
    task_received = 0
    plan_execute = 0
    subtask_done = 0
    last_ts = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = row.get("event")
                if event == "task_received":
                    task_received += 1
                elif event == "plan_execute":
                    plan_execute += 1
                elif event == "subtask_done":
                    subtask_done += 1
                last_ts = max(last_ts, str(row.get("ts") or ""))
    except OSError:
        pass
    return (task_received, plan_execute, subtask_done, last_ts)

best = None
best_score = (-1, -1, -1, "")
for path in candidates:
    item_score = score(path)
    if item_score > best_score:
        best = path
        best_score = item_score
if best is None:
    best = default
print(best.as_posix())
PY
}

SCHEDULER_LOG="$(resolve_scheduler_log)"
if [[ "$SCHEDULER_LOG" != "$LOGS_DIR/scheduler_events.jsonl" && -f "$SCHEDULER_LOG" ]]; then
  cp "$SCHEDULER_LOG" "$LOGS_DIR/scheduler_events.jsonl"
fi
echo "[export] scheduler event log: $SCHEDULER_LOG"

if [[ "$WAIT_COMPLETION" == "1" ]]; then
  echo "[export] waiting for mission completion from http://$DASH_HOST:$DASH_PORT/api/state"
  python3 - "$DASH_HOST" "$DASH_PORT" "$COMPLETION_TIMEOUT" "$COMPLETION_POLL_SEC" <<'PY'
import json, sys, time, urllib.request

host, port = sys.argv[1], sys.argv[2]
timeout_sec, poll_sec = float(sys.argv[3]), float(sys.argv[4])
url = f"http://{host}:{port}/api/state"
deadline = time.time() + timeout_sec
last_summary = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=min(10.0, max(1.0, poll_sec))) as resp:
            data = json.load(resp)
        m = data.get("metrics", {})
        pending = int(m.get("mission_tasks_pending", m.get("subtasks_pending", 0)) or 0)
        queued = int(m.get("mission_tasks_queued", m.get("subtasks_queued", 0)) or 0)
        running = int(m.get("mission_tasks_running", m.get("subtasks_running", 0)) or 0)
        failed = sum(
            1
            for item in data.get("subtasks", [])
            if not item.get("redundant") and item.get("status") == "FAILED"
        )
        done = sum(
            1
            for item in data.get("subtasks", [])
            if not item.get("redundant") and item.get("status") == "DONE"
        )
        last_summary = (
            f"mission_pending={pending} mission_queued={queued} "
            f"mission_running={running} mission_done={done} mission_failed={failed}"
        )
        if pending == 0 and queued == 0 and running == 0:
            print(f"ready: {last_summary}")
            raise SystemExit(0)
        print(f"waiting: {last_summary}", flush=True)
    except Exception as exc:
        last_summary = f"dashboard_error={exc}"
    time.sleep(max(0.2, poll_sec))
raise SystemExit(f"timeout waiting for mission completion: {last_summary}")
PY
fi

echo "[export] capturing scheduler state from http://$DASH_HOST:$DASH_PORT/api/state"
python3 - "$DASH_HOST" "$DASH_PORT" "$RUN_ROOT/visuals/final_state.json" "$CAPTURE_TIMEOUT" <<'PY'
import json, sys, time, urllib.request
host, port, out, timeout_sec = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
url = f"http://{host}:{port}/api/state"
deadline = time.time() + timeout_sec
last_err = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=min(10.0, timeout_sec)) as resp:
            data = json.load(resp)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(out)
        raise SystemExit(0)
    except Exception as exc:
        last_err = exc
        time.sleep(0.5)
raise SystemExit(f"failed to capture dashboard state: {last_err}")
PY

echo "[export] writing mission timeline summary"
python3 - "$SCHEDULER_LOG" "$RUN_ROOT/visuals/mission_timeline.txt" <<'PY'
import json, sys
from collections import OrderedDict
from pathlib import Path

log_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
tasks = OrderedDict()
if log_path.exists():
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "task_received":
            tid = row.get("task_id", "")
            if tid and tid not in tasks:
                tasks[tid] = {
                    "sent_at": row.get("ts") or row.get("created_at") or "",
                    "launches": row.get("launches", "?"),
                    "redundant_launches": row.get("redundant_launches", 0),
                }
lines = ["Mission runtime timeline"]
for idx, (tid, meta) in enumerate(tasks.items(), start=1):
    lines.append(
        f"wave={idx} task_id={tid} sent_at={meta['sent_at']} "
        f"launches={meta['launches']} redundant_launches={meta['redundant_launches']}"
    )
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(out_path)
PY

echo "[export] exporting planning data"
python3 scripts/export_planning_results.py \
  --scheduler-log "$SCHEDULER_LOG" \
  --depot-log "$LOGS_DIR/depot_events.jsonl" \
  --out "$RUN_ROOT/results/planning_results.json" >/dev/null

echo "[export] exporting wave dispatch metrics"
python3 scripts/export_wave_dispatch_metrics.py \
  --scheduler-log "$SCHEDULER_LOG" \
  --out-json "$RUN_ROOT/results/wave_dispatch_metrics.json" \
  --out-txt "$RUN_ROOT/results/leader_package/05_wave_dispatch_metrics.txt" >/dev/null

echo "[export] exporting action audit"
python3 scripts/export_action_audit.py \
  --scheduler-log "$SCHEDULER_LOG" \
  --depot-log "$LOGS_DIR/depot_events.jsonl" \
  --planning-json "$RUN_ROOT/results/planning_results.json" \
  --final-state "$RUN_ROOT/visuals/final_state.json" \
  --out-json "$RUN_ROOT/results/action_audit.json" \
  --out-txt "$RUN_ROOT/results/leader_package/06_action_audit.txt" >/dev/null

echo "[export] exporting LAN link evidence"
python3 scripts/export_lan_link_evidence.py \
  --lan-log "$LOGS_DIR/lan_events.jsonl" \
  --out-json "$RUN_ROOT/results/lan_link_evidence.json" \
  --out-txt "$RUN_ROOT/results/leader_package/10_lan_link_evidence.txt" >/dev/null

echo "[export] exporting standard interface files"
python3 scripts/export_standard_interfaces.py \
  --scheduler-log "$SCHEDULER_LOG" \
  --planning-json "$RUN_ROOT/results/planning_results.json" \
  --final-state "$RUN_ROOT/visuals/final_state.json" \
  --out-dir "$RUN_ROOT/results" >/dev/null

echo "[export] exporting leader package"
python3 scripts/export_leader_package.py \
  --run-root "$RUN_ROOT" \
  --logs-dir "$LOGS_DIR" >/dev/null

echo
echo "[export] planning data  : $RUN_ROOT/results/planning_results.json"
echo "[export] stage timeline : $RUN_ROOT/results/stage_timeline.json"
echo "[export] assignment     : $RUN_ROOT/results/point_assignment.json"
echo "[export] realtime pose  : $RUN_ROOT/results/realtime_pose.json"
echo "[export] dispatch metric: $RUN_ROOT/results/wave_dispatch_metrics.json"
echo "[export] action audit   : $RUN_ROOT/results/action_audit.json"
echo "[export] LAN evidence   : $RUN_ROOT/results/lan_link_evidence.json"
echo "[export] leader package : $RUN_ROOT/results/leader_package"
echo "[export] run brief      : $RUN_ROOT/results/leader_package/01_run_brief.txt"

python3 - "$RUN_ROOT" <<'PY'
import json
import math
import sys
from pathlib import Path

run_root = Path(sys.argv[1])


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def count_list(path: Path) -> int:
    obj = load_json(path, [])
    return len(obj) if isinstance(obj, list) else 0


def fmt_sec(value) -> str:
    try:
        num = float(value)
    except Exception:
        return "-"
    if not math.isfinite(num):
        return "-"
    return f"{num:.3f}s"


final_state = load_json(run_root / "visuals" / "final_state.json", {})
metrics = final_state.get("metrics", {}) if isinstance(final_state, dict) else {}
audit = load_json(run_root / "results" / "action_audit.json", {})
summary = audit.get("summary", {}) if isinstance(audit, dict) else {}
issues = audit.get("issues", []) if isinstance(audit, dict) else []
wave_metrics = load_json(run_root / "results" / "wave_dispatch_metrics.json", {})
waves = wave_metrics.get("waves", []) if isinstance(wave_metrics, dict) else []
planning = load_json(run_root / "results" / "planning_results.json", {})

mission_total = int(summary.get("mission_tasks_total", metrics.get("mission_tasks_total", 0)) or 0)
mission_done = int(summary.get("mission_done", 0) or 0)
redundant_total = int(summary.get("redundant_tasks_total", metrics.get("redundant_tasks_total", 0)) or 0)
redundant_done = int(summary.get("redundant_done", 0) or 0)
all_total = int(summary.get("subtasks_total", metrics.get("subtasks_total", 0)) or 0)
all_done = mission_done + redundant_done
failed = sum(1 for item in final_state.get("subtasks", []) if item.get("status") == "FAILED") if isinstance(final_state, dict) else 0
pending = int(metrics.get("subtasks_pending", 0) or 0)
queued = int(metrics.get("subtasks_queued", 0) or 0)
running = int(metrics.get("subtasks_running", 0) or 0)

dispatch_values = [
    float(row.get("mission_dispatch_wall_sec"))
    for row in waves
    if row.get("mission_dispatch_wall_sec") is not None
]
dispatch_max = max(dispatch_values) if dispatch_values else None
dispatch_avg = sum(dispatch_values) / len(dispatch_values) if dispatch_values else None
dispatch_ok = dispatch_max is not None and dispatch_max <= 10.0

fire_error = summary.get("fire_error_abs_max_sec")
vehicles_total = int(metrics.get("vehicles_total", 0) or 0)
vehicles_online = int(metrics.get("vehicles_online", 0) or 0)
plan_count = int(planning.get("plan_count", 0) or 0) if isinstance(planning, dict) else 0
event_counts = summary.get("event_counts", {}) if isinstance(summary, dict) else {}
depot_event_counts = summary.get("depot_event_counts", {}) if isinstance(summary, dict) else {}

print()
print("========== Export Summary ==========")
print(f"Run root: {run_root}")
print(f"Vehicles online: {vehicles_online}/{vehicles_total}")
print(
    "Mission tasks: "
    f"{mission_done}/{mission_total} done, "
    f"pending={pending}, queued={queued}, running={running}, failed={failed}"
)
print(f"Redundant tasks: {redundant_done}/{redundant_total} done")
print(f"All subtasks: {all_done}/{all_total} done")
print(f"Planning records: {plan_count}")
print(f"Stage timeline rows: {count_list(run_root / 'results' / 'stage_timeline.json')}")
print(f"Point assignment rows: {count_list(run_root / 'results' / 'point_assignment.json')}")
print(f"Realtime pose rows: {count_list(run_root / 'results' / 'realtime_pose.json')}")
lan_evidence = load_json(run_root / "results" / "lan_link_evidence.json", {})
lan_links = lan_evidence.get("links", []) if isinstance(lan_evidence, dict) else []
lan_ok = sum(1 for item in lan_links if item.get("observed"))
lan_total = len(lan_links)
print(f"LAN link evidence: {lan_ok}/{lan_total} expected links observed")
print(f"Max fire time error: {fmt_sec(fire_error)}")
print(
    "Wave dispatch time: "
    f"waves={len(waves)}, max={fmt_sec(dispatch_max)}, avg={fmt_sec(dispatch_avg)}, "
    f"10s_check={'PASS' if dispatch_ok else 'WARN'}"
)
if waves:
    print("Per-wave dispatch:")
    for row in waves:
        idx = row.get("wave_index", "-")
        task_id = row.get("task_id", "-")
        sec = fmt_sec(row.get("mission_dispatch_wall_sec"))
        miss = row.get("mission_missing_count", 0)
        print(f"  wave {idx}: {task_id} dispatch={sec}, missing={miss}")
print(
    "Path/conflict counters: "
    f"path_rejected={metrics.get('path_rejected', event_counts.get('path_rejected', 0))}, "
    f"proposal_retries={metrics.get('proposal_retries', event_counts.get('path_request_retry', 0))}, "
    f"deadlock_risk={metrics.get('deadlock_risk', event_counts.get('reservation_deadlock_risk', 0))}"
)
print(
    "Depot counters: "
    f"reload_started={metrics.get('reload_started', event_counts.get('depot_reload_start', 0))}, "
    f"reload_complete={event_counts.get('depot_reload_complete', 0)}, "
    f"depot_score_queries={summary.get('depot_score_queries', depot_event_counts.get('depot_score_computed', 0))}, "
    f"depot_assignment_results={summary.get('depot_assignment_results', depot_event_counts.get('depot_assignment_computed', 0))}"
)
print(f"Audit issues: {len(issues)}")
if issues:
    print("Issue samples:")
    for item in issues[:5]:
        print(f"  - {item}")
print("Key files:")
print(f"  {run_root / 'results' / 'leader_package' / '01_run_brief.txt'}")
print(f"  {run_root / 'results' / 'leader_package' / '05_wave_dispatch_metrics.txt'}")
print(f"  {run_root / 'results' / 'leader_package' / '06_action_audit.txt'}")
print(f"  {run_root / 'results' / 'leader_package' / '10_lan_link_evidence.txt'}")
print("==================================================")
PY
