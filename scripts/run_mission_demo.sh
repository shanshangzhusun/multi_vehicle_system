#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="configs/shp_four_wave_demo.json"
OUT_ROOT="mission_demo_beijing"
ROADS_SHP="luwang_data/china-240101-free.shp/gis_osm_roads_free_1.shp"
BBOX="116.24,39.84,116.43,39.97"
VEHICLES=8
SNAPSHOT_DELAY=4
WAIT_AFTER_FINAL_SEND=15
WAIT_UNTIL_ALL_DONE=0
COMPLETION_TIMEOUT_SEC="600"
COMPLETION_POLL_SEC="2"
STOP_AT_END=0
PREPARE_ONLY=0
WAVES=()
FIRE_ZONE_ENABLED="true"
FIRE_ZONE_AXIS="x"
FIRE_ZONE_SIDE="east"
FIRE_ZONE_BONUS="25.0"
DASH_READY_TIMEOUT="120"
CAPTURE_TIMEOUT="30"
RENDER_BASE_PREVIEW=0
RENDER_MAP_ELEMENTS=0
RENDER_SNAPSHOTS=0
SCHED_TICK_SEC="0.4"
HEARTBEAT_TIMEOUT_SEC="5.0"
PROPOSAL_TIMEOUT_SEC="4.0"
MAX_PROPOSAL_RETRIES="2"
RESERVATION_SLOT_SEC="1"
RESERVATION_DELAY_STEP_SEC="2.0"
RESERVATION_MAX_DELAY_SEC="180.0"
RESERVATION_RETENTION_SEC="30.0"
RESERVATION_DEADLOCK_BACKOFF_SEC="5.0"
VEHICLE_REALTIME_SCALE="0.08"
SIMULATION_MODE="realtime"
SETTLE_DELAY_SEC="0.5"
DEPOT_CAPACITY="1"
RELOAD_DURATION_SEC="60.0"
HIDE_TRIGGER_SLACK_SEC="60.0"
HIDE_MIN_WAIT_SEC="30.0"
HOT_DISTANCE_THRESHOLD_M="1500.0"
COLD_DISTANCE_THRESHOLD_M="4500.0"
HOT_STARTUP_SEC="30.0"
COLD_STARTUP_SEC="180.0"

usage() {
  cat <<'USAGE'
Usage: scripts/run_mission_demo.sh [options]

Options:
  --config <path>            JSON config path (default: configs/shp_four_wave_demo.json)
  --out-root <dir>           Demo root output directory (default: mission_demo_beijing)
  --roads-shp <path>         Roads shapefile path
  --bbox <minx,miny,maxx,maxy>
                             Region bbox in lon/lat (default: Beijing core)
  --vehicles <n>             Number of vehicles to start (default: 8)
  --snapshot-delay <sec>     Delay after each wave send before capturing state (default: 4)
  --wait-after-final <sec>   Delay after final wave before final snapshot (default: 15)
  --completion-timeout <sec> Max real wait for all subtasks to finish before export (default: 600)
  --simulation-mode <mode>   Simulation mode passed to generated configs (realtime/discrete_event)
  --vehicle-realtime-scale <factor>
                             Vehicle execution time scale for realtime mode
  --prepare-only             Only generate map/config/runtime data, do not start processes
  --stop-at-end              Stop scheduler and vehicles after captures
  -h, --help                 Show this help
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
path, expr = sys.argv[1], sys.argv[2]
obj = json.load(open(path, "r", encoding="utf-8"))
cur = obj
try:
    for part in expr.split('.'):
        cur = cur[part]
except Exception:
    print("")
    raise SystemExit(0)
print(cur)
PY
}

now_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

load_config() {
  local cfg="$1"
  [[ -f "$cfg" ]] || { echo "Config not found: $cfg" >&2; exit 1; }

  OUT_ROOT="$(json_get "$cfg" "out_root")"
  ROADS_SHP="$(json_get "$cfg" "roads_shp")"
  BBOX="$(json_get "$cfg" "bbox")"
  VEHICLES="$(json_get "$cfg" "vehicles")"
  HIDE_POINT_COUNT="$(json_try_get "$cfg" "hide_point_count")"
  [[ -z "$HIDE_POINT_COUNT" ]] && HIDE_POINT_COUNT="0"
  LAUNCH_POINT_COUNT="$(json_try_get "$cfg" "launch_point_count")"
  [[ -z "$LAUNCH_POINT_COUNT" ]] && LAUNCH_POINT_COUNT="0"
  VEHICLE_PORT_BASE="$(json_try_get "$cfg" "vehicle_port_base")"
  [[ -z "$VEHICLE_PORT_BASE" ]] && VEHICLE_PORT_BASE="12100"
  VEHICLE_PORTS="$(python3 - "$cfg" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1], "r", encoding="utf-8"))
ports = obj.get("vehicle_ports") or []
if isinstance(ports, list):
    print(",".join(str(int(p)) for p in ports))
PY
)"
  SCHEDULER_PORT="$(json_try_get "$cfg" "scheduler_port")"
  [[ -z "$SCHEDULER_PORT" ]] && SCHEDULER_PORT="9120"
  DASHBOARD_PORT="$(json_try_get "$cfg" "dashboard_port")"
  [[ -z "$DASHBOARD_PORT" ]] && DASHBOARD_PORT="19120"
  SNAPSHOT_DELAY="$(json_get "$cfg" "snapshot_delay_sec")"
  WAIT_AFTER_FINAL_SEND="$(json_get "$cfg" "wait_after_final_sec")"
  wait_until_all_done_cfg="$(json_try_get "$cfg" "wait_until_all_done")"
  if [[ "$wait_until_all_done_cfg" == "False" || "$wait_until_all_done_cfg" == "false" ]]; then
    WAIT_UNTIL_ALL_DONE=0
  else
    WAIT_UNTIL_ALL_DONE=1
  fi
  COMPLETION_TIMEOUT_SEC="$(json_try_get "$cfg" "completion_timeout_sec")"
  [[ -z "$COMPLETION_TIMEOUT_SEC" ]] && COMPLETION_TIMEOUT_SEC="600"
  COMPLETION_POLL_SEC="$(json_try_get "$cfg" "completion_poll_sec")"
  [[ -z "$COMPLETION_POLL_SEC" ]] && COMPLETION_POLL_SEC="2"
  if [[ "$(json_get "$cfg" "stop_at_end")" == "True" || "$(json_get "$cfg" "stop_at_end")" == "true" ]]; then
    STOP_AT_END=1
  else
    STOP_AT_END=0
  fi

  if python3 - "$cfg" <<'PY' >/dev/null 2>&1
import json, sys
cfg=json.load(open(sys.argv[1], "r", encoding="utf-8"))
raise SystemExit(0 if "fire_zone" in cfg else 1)
PY
  then
    FIRE_ZONE_ENABLED="$(json_get "$cfg" "fire_zone.enabled")"
    FIRE_ZONE_AXIS="$(json_get "$cfg" "fire_zone.axis")"
    FIRE_ZONE_SIDE="$(json_get "$cfg" "fire_zone.side")"
    FIRE_ZONE_BONUS="$(json_get "$cfg" "fire_zone.bonus_score")"
  fi

  DASH_READY_TIMEOUT="$(json_try_get "$cfg" "dashboard_ready_timeout_sec")"
  [[ -z "$DASH_READY_TIMEOUT" ]] && DASH_READY_TIMEOUT="120"
  CAPTURE_TIMEOUT="$(json_try_get "$cfg" "capture_timeout_sec")"
  [[ -z "$CAPTURE_TIMEOUT" ]] && CAPTURE_TIMEOUT="30"
  RENDER_BASE_PREVIEW="$(json_try_get "$cfg" "render.base_preview")"
  [[ "$RENDER_BASE_PREVIEW" == "True" ]] && RENDER_BASE_PREVIEW="true"
  [[ "$RENDER_BASE_PREVIEW" == "False" ]] && RENDER_BASE_PREVIEW="false"
  [[ "$RENDER_BASE_PREVIEW" == "true" ]] && RENDER_BASE_PREVIEW=1 || RENDER_BASE_PREVIEW=0
  RENDER_MAP_ELEMENTS="$(json_try_get "$cfg" "render.map_elements")"
  [[ "$RENDER_MAP_ELEMENTS" == "True" ]] && RENDER_MAP_ELEMENTS="true"
  [[ "$RENDER_MAP_ELEMENTS" == "False" ]] && RENDER_MAP_ELEMENTS="false"
  [[ "$RENDER_MAP_ELEMENTS" == "true" ]] && RENDER_MAP_ELEMENTS=1 || RENDER_MAP_ELEMENTS=0
  RENDER_SNAPSHOTS="$(json_try_get "$cfg" "render.snapshots")"
  [[ "$RENDER_SNAPSHOTS" == "True" ]] && RENDER_SNAPSHOTS="true"
  [[ "$RENDER_SNAPSHOTS" == "False" ]] && RENDER_SNAPSHOTS="false"
  [[ "$RENDER_SNAPSHOTS" == "true" ]] && RENDER_SNAPSHOTS=1 || RENDER_SNAPSHOTS=0
  SCHED_TICK_SEC="$(json_try_get "$cfg" "scheduler_tuning.tick_sec")"
  [[ -z "$SCHED_TICK_SEC" ]] && SCHED_TICK_SEC="0.4"
  HEARTBEAT_TIMEOUT_SEC="$(json_try_get "$cfg" "scheduler_tuning.heartbeat_timeout_sec")"
  [[ -z "$HEARTBEAT_TIMEOUT_SEC" ]] && HEARTBEAT_TIMEOUT_SEC="5.0"
  PROPOSAL_TIMEOUT_SEC="$(json_try_get "$cfg" "scheduler_tuning.proposal_timeout_sec")"
  [[ -z "$PROPOSAL_TIMEOUT_SEC" ]] && PROPOSAL_TIMEOUT_SEC="4.0"
  MAX_PROPOSAL_RETRIES="$(json_try_get "$cfg" "scheduler_tuning.max_proposal_retries")"
  [[ -z "$MAX_PROPOSAL_RETRIES" ]] && MAX_PROPOSAL_RETRIES="2"
  RESERVATION_SLOT_SEC="$(json_try_get "$cfg" "scheduler_tuning.reservation_slot_sec")"
  [[ -z "$RESERVATION_SLOT_SEC" ]] && RESERVATION_SLOT_SEC="1"
  RESERVATION_DELAY_STEP_SEC="$(json_try_get "$cfg" "scheduler_tuning.reservation_delay_step_sec")"
  [[ -z "$RESERVATION_DELAY_STEP_SEC" ]] && RESERVATION_DELAY_STEP_SEC="2.0"
  RESERVATION_MAX_DELAY_SEC="$(json_try_get "$cfg" "scheduler_tuning.reservation_max_delay_sec")"
  [[ -z "$RESERVATION_MAX_DELAY_SEC" ]] && RESERVATION_MAX_DELAY_SEC="180.0"
  RESERVATION_RESERVE_WAIT_NODES="$(json_try_get "$cfg" "scheduler_tuning.reservation_reserve_wait_nodes")"
  [[ -z "$RESERVATION_RESERVE_WAIT_NODES" ]] && RESERVATION_RESERVE_WAIT_NODES="false"
  RESERVATION_RETENTION_SEC="$(json_try_get "$cfg" "scheduler_tuning.reservation_retention_sec")"
  [[ -z "$RESERVATION_RETENTION_SEC" ]] && RESERVATION_RETENTION_SEC="30.0"
  RESERVATION_DEADLOCK_BACKOFF_SEC="$(json_try_get "$cfg" "scheduler_tuning.reservation_deadlock_backoff_sec")"
  [[ -z "$RESERVATION_DEADLOCK_BACKOFF_SEC" ]] && RESERVATION_DEADLOCK_BACKOFF_SEC="5.0"
  MAX_ROUTE_CANDIDATES="$(json_try_get "$cfg" "scheduler_tuning.max_route_candidates")"
  [[ -z "$MAX_ROUTE_CANDIDATES" ]] && MAX_ROUTE_CANDIDATES="20"
  MAX_ROUTE_CANDIDATES_REAL="$(json_try_get "$cfg" "scheduler_tuning.max_route_candidates_real")"
  [[ -z "$MAX_ROUTE_CANDIDATES_REAL" ]] && MAX_ROUTE_CANDIDATES_REAL="$MAX_ROUTE_CANDIDATES"
  MAX_ROUTE_CANDIDATES_RESERVED="$(json_try_get "$cfg" "scheduler_tuning.max_route_candidates_reserved")"
  [[ -z "$MAX_ROUTE_CANDIDATES_RESERVED" ]] && MAX_ROUTE_CANDIDATES_RESERVED="12"
  MAX_ROUTE_CANDIDATES_REDUNDANT="$(json_try_get "$cfg" "scheduler_tuning.max_route_candidates_redundant")"
  [[ -z "$MAX_ROUTE_CANDIDATES_REDUNDANT" ]] && MAX_ROUTE_CANDIDATES_REDUNDANT="6"
  MAX_ACTIVE_REAL_PLANNING_SUBTASKS="$(json_try_get "$cfg" "scheduler_tuning.max_active_real_planning_subtasks")"
  [[ -z "$MAX_ACTIVE_REAL_PLANNING_SUBTASKS" ]] && MAX_ACTIVE_REAL_PLANNING_SUBTASKS="0"
  MAX_ACTIVE_REDUNDANT_PLANNING_SUBTASKS="$(json_try_get "$cfg" "scheduler_tuning.max_active_redundant_planning_subtasks")"
  [[ -z "$MAX_ACTIVE_REDUNDANT_PLANNING_SUBTASKS" ]] && MAX_ACTIVE_REDUNDANT_PLANNING_SUBTASKS="0"
  MAX_ACTIVE_REAL_PLANNING_PER_TASK="$(json_try_get "$cfg" "scheduler_tuning.max_active_real_planning_per_task")"
  [[ -z "$MAX_ACTIVE_REAL_PLANNING_PER_TASK" ]] && MAX_ACTIVE_REAL_PLANNING_PER_TASK="0"
  MAX_ACTIVE_REDUNDANT_PLANNING_PER_TASK="$(json_try_get "$cfg" "scheduler_tuning.max_active_redundant_planning_per_task")"
  [[ -z "$MAX_ACTIVE_REDUNDANT_PLANNING_PER_TASK" ]] && MAX_ACTIVE_REDUNDANT_PLANNING_PER_TASK="0"
  MAX_ACTIVE_REAL_TASK_GROUPS="$(json_try_get "$cfg" "scheduler_tuning.max_active_real_task_groups")"
  [[ -z "$MAX_ACTIVE_REAL_TASK_GROUPS" ]] && MAX_ACTIVE_REAL_TASK_GROUPS="0"
  MAX_ACTIVE_REDUNDANT_TASK_GROUPS="$(json_try_get "$cfg" "scheduler_tuning.max_active_redundant_task_groups")"
  [[ -z "$MAX_ACTIVE_REDUNDANT_TASK_GROUPS" ]] && MAX_ACTIVE_REDUNDANT_TASK_GROUPS="0"
  VEHICLE_PLATFORM_SCORE_ENABLED="$(json_try_get "$cfg" "scheduler_tuning.vehicle_platform_score_enabled")"
  [[ -z "$VEHICLE_PLATFORM_SCORE_ENABLED" ]] && VEHICLE_PLATFORM_SCORE_ENABLED="true"
  VEHICLE_PLATFORM_SCORE_REQUEST_LIMIT="$(json_try_get "$cfg" "scheduler_tuning.vehicle_platform_score_request_limit_per_subtask")"
  [[ -z "$VEHICLE_PLATFORM_SCORE_REQUEST_LIMIT" ]] && VEHICLE_PLATFORM_SCORE_REQUEST_LIMIT="24"
  PROJECT_BOOK_ENABLED="$(json_try_get "$cfg" "scheduler_tuning.project_book_enabled")"
  [[ -z "$PROJECT_BOOK_ENABLED" ]] && PROJECT_BOOK_ENABLED="true"
  DEPOT_BOOK_ENABLED="$(json_try_get "$cfg" "scheduler_tuning.depot_book_enabled")"
  [[ -z "$DEPOT_BOOK_ENABLED" ]] && DEPOT_BOOK_ENABLED="true"
  VEHICLE_REALTIME_SCALE="$(json_try_get "$cfg" "vehicle_tuning.realtime_scale")"
  [[ -z "$VEHICLE_REALTIME_SCALE" ]] && VEHICLE_REALTIME_SCALE="0.08"
  VEHICLE_SPEED_MPS="$(json_try_get "$cfg" "vehicle_tuning.speed_mps")"
  [[ -z "$VEHICLE_SPEED_MPS" ]] && VEHICLE_SPEED_MPS="22.222"
  SIMULATION_MODE="$(json_try_get "$cfg" "simulation.mode")"
  [[ -z "$SIMULATION_MODE" ]] && SIMULATION_MODE="realtime"
  SETTLE_DELAY_SEC="$(json_try_get "$cfg" "simulation.settle_delay_sec")"
  [[ -z "$SETTLE_DELAY_SEC" ]] && SETTLE_DELAY_SEC="0.5"
  DEPOT_CAPACITY="$(json_try_get "$cfg" "depot.capacity")"
  [[ -z "$DEPOT_CAPACITY" ]] && DEPOT_CAPACITY="1"
  RELOAD_DURATION_SEC="$(json_try_get "$cfg" "depot.reload_duration_sec")"
  [[ -z "$RELOAD_DURATION_SEC" ]] && RELOAD_DURATION_SEC="60.0"
  REDUNDANCY_ENABLED="$(json_try_get "$cfg" "redundancy.enabled")"
  [[ -z "$REDUNDANCY_ENABLED" ]] && REDUNDANCY_ENABLED="false"
  REDUNDANCY_RATIO="$(json_try_get "$cfg" "redundancy.ratio")"
  [[ -z "$REDUNDANCY_RATIO" ]] && REDUNDANCY_RATIO="0.2"
  HIDE_TRIGGER_SLACK_SEC="$(json_try_get "$cfg" "hide_strategy.trigger_slack_sec")"
  [[ -z "$HIDE_TRIGGER_SLACK_SEC" ]] && HIDE_TRIGGER_SLACK_SEC="60.0"
  HIDE_MIN_WAIT_SEC="$(json_try_get "$cfg" "hide_strategy.min_wait_sec")"
  [[ -z "$HIDE_MIN_WAIT_SEC" ]] && HIDE_MIN_WAIT_SEC="30.0"
  HOT_DISTANCE_THRESHOLD_M="$(json_try_get "$cfg" "launch_timing.hot_distance_threshold_m")"
  [[ -z "$HOT_DISTANCE_THRESHOLD_M" ]] && HOT_DISTANCE_THRESHOLD_M="5000.0"
  COLD_DISTANCE_THRESHOLD_M="$(json_try_get "$cfg" "launch_timing.cold_distance_threshold_m")"
  [[ -z "$COLD_DISTANCE_THRESHOLD_M" ]] && COLD_DISTANCE_THRESHOLD_M="5000.0"
  LAUNCH_PREPARE_SEC="$(json_try_get "$cfg" "launch_timing.launch_prepare_sec")"
  [[ -z "$LAUNCH_PREPARE_SEC" ]] && LAUNCH_PREPARE_SEC="300.0"
  HOT_STANDBY_SEC="$(json_try_get "$cfg" "launch_timing.hot_standby_sec")"
  [[ -z "$HOT_STANDBY_SEC" ]] && HOT_STANDBY_SEC="180.0"
  COLD_STANDBY_SEC="$(json_try_get "$cfg" "launch_timing.cold_standby_sec")"
  [[ -z "$COLD_STANDBY_SEC" ]] && COLD_STANDBY_SEC="420.0"
  HOT_STARTUP_SEC="$(json_try_get "$cfg" "launch_timing.hot_startup_sec")"
  [[ -z "$HOT_STARTUP_SEC" ]] && HOT_STARTUP_SEC="$HOT_STANDBY_SEC"
  COLD_STARTUP_SEC="$(json_try_get "$cfg" "launch_timing.cold_startup_sec")"
  [[ -z "$COLD_STARTUP_SEC" ]] && COLD_STARTUP_SEC="$COLD_STANDBY_SEC"

  mapfile -t WAVES < <(python3 - "$cfg" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
waves = cfg.get("waves", [])
if len(waves) < 1:
    raise SystemExit("config waves must contain at least 1 item")
for idx, wave in enumerate(waves, start=1):
    task_id = wave.get("task_id", f"task_wave_{idx:02d}")
    count = int(wave["count"])
    start_after = int(wave["start_after_sec"])
    interval = int(wave["interval_sec"])
    same_fire_time = bool(wave.get("same_fire_time", True))
    gap = wave.get("gap_after_send_sec", "")
    virtual_offset = sum(float(waves[j].get("gap_after_send_sec", 0) or 0) for j in range(idx - 1))
    print(f"{task_id}|{count}|{start_after}|{interval}|{int(same_fire_time)}|{gap}|{virtual_offset}")
PY
  )
}

wait_for_dashboard() {
  local host="$1"
  local port="$2"
  local vehicles="$3"
  local timeout_sec="$4"
  python3 - "$host" "$port" "$vehicles" "$timeout_sec" <<'PY'
import json, sys, time, urllib.request
host, port, vehicles, timeout_sec = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
url = f"http://{host}:{port}/api/state"
deadline = time.time() + timeout_sec
last_err = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.load(resp)
        online = int(data.get("metrics", {}).get("vehicles_online", 0))
        if online >= vehicles:
            print(f"ready: vehicles_online={online}")
            raise SystemExit(0)
        last_err = f"vehicles_online={online}"
    except Exception as exc:
        last_err = str(exc)
    time.sleep(0.5)
raise SystemExit(f"scheduler not ready in time: {last_err}")
PY
}

wait_for_completion() {
  local host="$1"
  local port="$2"
  local timeout_sec="$3"
  local poll_sec="$4"
  python3 - "$host" "$port" "$timeout_sec" "$poll_sec" <<'PY'
import json, sys, time, urllib.request

host, port = sys.argv[1], int(sys.argv[2])
timeout_sec = float(sys.argv[3])
poll_sec = max(0.2, float(sys.argv[4]))
url = f"http://{host}:{port}/api/state"
deadline = time.time() + timeout_sec
loop_start = time.time()
last_snapshot = None
last_err = None
mission_plan_ready_sec = None
mission_dispatch_ready_sec = None

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        metrics = data.get("metrics", {})
        pending = int(metrics.get("subtasks_pending", 0))
        running = int(metrics.get("subtasks_running", 0))
        total = int(metrics.get("subtasks_total", 0))
        mission = int(metrics.get("mission_tasks_total", total))
        redundant = int(metrics.get("redundant_tasks_total", 0))
        mission_pending = int(metrics.get("mission_tasks_pending", pending))
        mission_running = int(metrics.get("mission_tasks_running", running))
        mission_dispatch_pending = int(metrics.get("mission_tasks_dispatch_pending", mission_pending))
        mission_planning_pending = int(metrics.get("mission_tasks_planning_pending", mission_pending + mission_running))
        redundant_pending = int(metrics.get("redundant_tasks_pending", 0))
        redundant_running = int(metrics.get("redundant_tasks_running", 0))
        if mission > 0 and mission_planning_pending == 0 and mission_plan_ready_sec is None:
            mission_plan_ready_sec = max(0.0, time.time() - loop_start)
        if mission > 0 and mission_dispatch_pending == 0 and mission_dispatch_ready_sec is None:
            mission_dispatch_ready_sec = max(0.0, time.time() - loop_start)
        last_snapshot = (mission, redundant, mission_pending, mission_running, redundant_pending, redundant_running)
        if total > 0 and pending == 0 and running == 0:
            print(
                f"all_done mission_tasks={mission} redundant_tasks={redundant} "
                f"mission_pending={mission_pending} mission_running={mission_running} "
                f"redundant_pending={redundant_pending} redundant_running={redundant_running} "
                f"mission_dispatch_ready_sec={-1.0 if mission_dispatch_ready_sec is None else mission_dispatch_ready_sec:.3f} "
                f"mission_plan_ready_sec={-1.0 if mission_plan_ready_sec is None else mission_plan_ready_sec:.3f}"
            )
            raise SystemExit(0)
        last_err = (
            f"mission_tasks={mission} redundant_tasks={redundant} "
            f"mission_pending={mission_pending} mission_running={mission_running} "
            f"redundant_pending={redundant_pending} redundant_running={redundant_running}"
        )
    except Exception as exc:
        if isinstance(exc, SystemExit):
            raise
        last_err = str(exc)
    time.sleep(poll_sec)

if last_snapshot is not None:
    mission, redundant, mission_pending, mission_running, redundant_pending, redundant_running = last_snapshot
    print(
        f"timeout mission_tasks={mission} redundant_tasks={redundant} "
        f"mission_pending={mission_pending} mission_running={mission_running} "
        f"redundant_pending={redundant_pending} redundant_running={redundant_running} "
        f"mission_dispatch_ready_sec={-1.0 if mission_dispatch_ready_sec is None else mission_dispatch_ready_sec:.3f} "
        f"mission_plan_ready_sec={-1.0 if mission_plan_ready_sec is None else mission_plan_ready_sec:.3f}"
    )
else:
    print(f"timeout error={last_err}")
raise SystemExit(1)
PY
}

sleep_for_mode() {
  local requested="$1"
  local mode="$2"
  local settle="$3"
  python3 - "$requested" "$mode" "$settle" <<'PY'
import sys, time
requested = float(sys.argv[1])
mode = sys.argv[2].strip().lower()
settle = max(0.0, float(sys.argv[3]))
if requested <= 0:
    raise SystemExit(0)
if mode == "discrete_event":
    time.sleep(max(0.01, settle))
else:
    time.sleep(requested)
PY
}

generate_wave_task_file() {
  local wave_num="$1"
  local task_id="$2"
  local count="$3"
  local start_after="$4"
  local interval="$5"
  local same_fire_time="$6"
  local task_file="$7"

  local gen_args=(
    python3 scripts/gen_task.py
    --out "$task_file"
    --task-id "$task_id"
    --count "$count"
    --start-after-sec "$start_after"
    --interval-sec "$interval"
  )
  if [[ "$same_fire_time" == "1" ]]; then
    gen_args+=(--same-fire-time)
  fi
  "${gen_args[@]}" >/dev/null
}

generate_all_wave_tasks() {
  mkdir -p "$OUT_ROOT/tasks"
  for idx in "${!WAVES[@]}"; do
    local wave_num=$((idx + 1))
    local task_id count start_after interval same_fire_time gap virtual_offset
    IFS='|' read -r task_id count start_after interval same_fire_time gap virtual_offset <<<"${WAVES[$idx]}"
    printf -v wave_tag "%02d" "$wave_num"
    local task_file="$OUT_ROOT/tasks/task_wave_${wave_tag}.json"
    generate_wave_task_file "$wave_num" "$task_id" "$count" "$start_after" "$interval" "$same_fire_time" "$task_file"
  done
}

capture_state() {
  local dash_host="$1"
  local dash_port="$2"
  local out_json="$3"
  local out_png="$4"
  local timeout_sec="$5"
  python3 - "$dash_host" "$dash_port" "$out_json" "$timeout_sec" <<'PY'
import json, sys, time, urllib.request
host, port, out, timeout_sec = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
url = f"http://{host}:{port}/api/state"
deadline = time.time() + timeout_sec
last_err = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=min(15.0, timeout_sec)) as resp:
            data = json.load(resp)
        break
    except Exception as exc:
        last_err = exc
        time.sleep(1.0)
else:
    raise SystemExit(f"capture_state failed: {last_err}")
with open(out, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(out)
PY
  if [[ "$RENDER_SNAPSHOTS" == "1" ]]; then
    python3 scripts/render_state_snapshot.py --state-json "$out_json" --out "$out_png" >/dev/null
  fi
}

# First pass: read config path early so config can define defaults.
args=("$@")
idx=0
while [[ $idx -lt $# ]]; do
  case "${args[$idx]}" in
    --config)
      idx=$((idx+1))
      CONFIG_PATH="${args[$idx]}"
      ;;
  esac
  idx=$((idx+1))
done

load_config "$CONFIG_PATH"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      shift 2;;
    --out-root)
      OUT_ROOT="$2"; shift 2;;
    --roads-shp)
      ROADS_SHP="$2"; shift 2;;
    --bbox)
      BBOX="$2"; shift 2;;
    --vehicles)
      VEHICLES="$2"; shift 2;;
    --snapshot-delay)
      SNAPSHOT_DELAY="$2"; shift 2;;
    --wait-after-final)
      WAIT_AFTER_FINAL_SEND="$2"; shift 2;;
    --completion-timeout)
      COMPLETION_TIMEOUT_SEC="$2"; shift 2;;
    --simulation-mode)
      SIMULATION_MODE="$2"; shift 2;;
    --vehicle-realtime-scale)
      VEHICLE_REALTIME_SCALE="$2"; shift 2;;
    --prepare-only)
      PREPARE_ONLY=1; shift;;
    --stop-at-end)
      STOP_AT_END=1; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1;;
  esac
done

RUN_START_MS="$(now_ms)"
mkdir -p "$OUT_ROOT/visuals"
mkdir -p "$OUT_ROOT/results"
FINAL_WAIT_STATUS="fixed_wait"

BUILD_START_MS="$(now_ms)"
echo "[mission] preparing runtime under $OUT_ROOT"
BUILD_CMD=(python3 scripts/build_shp_region_demo.py \
  --roads-shp "$ROADS_SHP" \
  --bbox "$BBOX" \
  --vehicles "$VEHICLES" \
  --hide-point-count "$HIDE_POINT_COUNT" \
  --launch-point-count "$LAUNCH_POINT_COUNT" \
  --scheduler-port "$SCHEDULER_PORT" \
  --dashboard-port "$DASHBOARD_PORT" \
  --vehicle-port-base "$VEHICLE_PORT_BASE" \
  --vehicle-ports "$VEHICLE_PORTS" \
  --fire-zone-enabled "$FIRE_ZONE_ENABLED" \
  --fire-zone-axis "$FIRE_ZONE_AXIS" \
  --fire-zone-side "$FIRE_ZONE_SIDE" \
  --fire-zone-bonus "$FIRE_ZONE_BONUS" \
  --scheduler-tick-sec "$SCHED_TICK_SEC" \
  --heartbeat-timeout-sec "$HEARTBEAT_TIMEOUT_SEC" \
  --proposal-timeout-sec "$PROPOSAL_TIMEOUT_SEC" \
  --max-proposal-retries "$MAX_PROPOSAL_RETRIES" \
  --reservation-slot-sec "$RESERVATION_SLOT_SEC" \
  --reservation-delay-step-sec "$RESERVATION_DELAY_STEP_SEC" \
  --reservation-max-delay-sec "$RESERVATION_MAX_DELAY_SEC" \
  --reservation-retention-sec "$RESERVATION_RETENTION_SEC" \
  --reservation-deadlock-backoff-sec "$RESERVATION_DEADLOCK_BACKOFF_SEC" \
  --max-route-candidates "$MAX_ROUTE_CANDIDATES" \
  --max-route-candidates-real "$MAX_ROUTE_CANDIDATES_REAL" \
  --max-route-candidates-reserved "$MAX_ROUTE_CANDIDATES_RESERVED" \
  --max-route-candidates-redundant "$MAX_ROUTE_CANDIDATES_REDUNDANT" \
  --max-active-real-planning-subtasks "$MAX_ACTIVE_REAL_PLANNING_SUBTASKS" \
  --max-active-redundant-planning-subtasks "$MAX_ACTIVE_REDUNDANT_PLANNING_SUBTASKS" \
  --max-active-real-planning-per-task "$MAX_ACTIVE_REAL_PLANNING_PER_TASK" \
  --max-active-redundant-planning-per-task "$MAX_ACTIVE_REDUNDANT_PLANNING_PER_TASK" \
  --max-active-real-task-groups "$MAX_ACTIVE_REAL_TASK_GROUPS" \
  --max-active-redundant-task-groups "$MAX_ACTIVE_REDUNDANT_TASK_GROUPS" \
  --vehicle-platform-score-enabled "$VEHICLE_PLATFORM_SCORE_ENABLED" \
  --vehicle-platform-score-request-limit "$VEHICLE_PLATFORM_SCORE_REQUEST_LIMIT" \
  --project-book-enabled "$PROJECT_BOOK_ENABLED" \
  --depot-book-enabled "$DEPOT_BOOK_ENABLED" \
  --vehicle-realtime-scale "$VEHICLE_REALTIME_SCALE" \
  --vehicle-speed-mps "$VEHICLE_SPEED_MPS" \
  --simulation-mode "$SIMULATION_MODE" \
  --depot-capacity "$DEPOT_CAPACITY" \
  --reload-duration-sec "$RELOAD_DURATION_SEC" \
  --redundancy-enabled "$REDUNDANCY_ENABLED" \
  --redundancy-ratio "$REDUNDANCY_RATIO" \
  --hide-trigger-slack-sec "$HIDE_TRIGGER_SLACK_SEC" \
  --hide-min-wait-sec "$HIDE_MIN_WAIT_SEC" \
  --hot-distance-threshold-m "$HOT_DISTANCE_THRESHOLD_M" \
  --cold-distance-threshold-m "$COLD_DISTANCE_THRESHOLD_M" \
  --launch-prepare-sec "$LAUNCH_PREPARE_SEC" \
  --hot-standby-sec "$HOT_STANDBY_SEC" \
  --cold-standby-sec "$COLD_STANDBY_SEC" \
  --hot-startup-sec "$HOT_STARTUP_SEC" \
  --cold-startup-sec "$COLD_STARTUP_SEC" \
  --out-root "$OUT_ROOT")
if [[ "$RESERVATION_RESERVE_WAIT_NODES" == "true" || "$RESERVATION_RESERVE_WAIT_NODES" == "1" ]]; then
  BUILD_CMD+=(--reservation-reserve-wait-nodes)
fi
"${BUILD_CMD[@]}"
BUILD_END_MS="$(now_ms)"
generate_all_wave_tasks

if [[ "$PREPARE_ONLY" == "1" ]]; then
  echo
  echo "[mission] runtime prepared"
  echo "[mission] graph         : $OUT_ROOT/data/road_graph_shp_demo.json"
  echo "[mission] points        : $OUT_ROOT/data/special_points_shp_demo.json"
  echo "[mission] scheduler cfg : $OUT_ROOT/configs/scheduler_debug.json"
  echo "[mission] vehicles dir  : $OUT_ROOT/configs/vehicles"
  echo "[mission] tasks dir     : $OUT_ROOT/tasks"
  exit 0
fi

PREVIEW_START_MS="$(now_ms)"
if [[ "$RENDER_BASE_PREVIEW" == "1" ]]; then
  echo "[mission] rendering base preview"
  python3 scripts/render_shp_preview.py \
    --roads-shp "$ROADS_SHP" \
    --bbox "$BBOX" \
    --classes motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,residential,service,unclassified,living_street \
    --out "$OUT_ROOT/visuals/base_preview.png"
else
  echo "[mission] skipping base preview render"
fi
PREVIEW_END_MS="$(now_ms)"

MAP_ELEMENTS_START_MS="$(now_ms)"
if [[ "$RENDER_MAP_ELEMENTS" == "1" ]]; then
  echo "[mission] rendering map elements"
  python3 scripts/render_map_elements.py \
    --scheduler-config "$OUT_ROOT/configs/scheduler_debug.json" \
    --out "$OUT_ROOT/visuals/map_elements.png" >/dev/null
else
  echo "[mission] skipping map elements render"
fi
MAP_ELEMENTS_END_MS="$(now_ms)"

STOP_PREVIOUS_START_MS="$(now_ms)"
echo "[mission] stopping previous local stack"
scripts/stack_down.sh \
  --scheduler-config "$OUT_ROOT/configs/scheduler_debug.json" \
  --vehicles-dir "$OUT_ROOT/configs/vehicles" >/dev/null 2>&1 || true
rm -f logs/*.jsonl
STOP_PREVIOUS_END_MS="$(now_ms)"

STACK_START_MS="$(now_ms)"
echo "[mission] starting scheduler + vehicles"
scripts/stack_up.sh \
  --scheduler-config "$OUT_ROOT/configs/scheduler_debug.json" \
  --vehicles-dir "$OUT_ROOT/configs/vehicles"
STACK_END_MS="$(now_ms)"

SCHED_CFG="$OUT_ROOT/configs/scheduler_debug.json"
SCHED_HOST="$(json_get "$SCHED_CFG" "listen_host")"
SCHED_PORT="$(json_get "$SCHED_CFG" "listen_port")"
DASH_HOST="$(json_get "$SCHED_CFG" "dashboard_host")"
DASH_PORT="$(json_get "$SCHED_CFG" "dashboard_port")"
if [[ "$SCHED_HOST" == "0.0.0.0" || -z "$SCHED_HOST" ]]; then
  SCHED_HOST="127.0.0.1"
fi
if [[ "$DASH_HOST" == "0.0.0.0" || -z "$DASH_HOST" ]]; then
  DASH_HOST="127.0.0.1"
fi

READY_START_MS="$(now_ms)"
echo "[mission] waiting for scheduler dashboard and vehicle heartbeats"
wait_for_dashboard "$DASH_HOST" "$DASH_PORT" "$VEHICLES" "$DASH_READY_TIMEOUT"
READY_END_MS="$(now_ms)"

TIMELINE_FILE="$OUT_ROOT/visuals/mission_timeline.txt"
{
  echo "Mission demo timeline"
  echo "out_root=$OUT_ROOT"
  echo "bbox=$BBOX"
  echo "vehicles=$VEHICLES"
  echo
} > "$TIMELINE_FILE"

SCHEDULING_START_MS="$(now_ms)"
WAVE_PHASE_START_MS="$SCHEDULING_START_MS"
WAVE_SEND_TOTAL_MS=0
SNAPSHOT_TOTAL_MS=0
SIM_WAIT_TOTAL_MS=0
VIRTUAL_BASE_ISO=""
if [[ "$SIMULATION_MODE" == "discrete_event" ]]; then
  VIRTUAL_BASE_ISO="$(python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
PY
)"
fi

for idx in "${!WAVES[@]}"; do
  wave_num=$((idx + 1))
  IFS='|' read -r task_id count start_after interval same_fire_time gap virtual_offset <<<"${WAVES[$idx]}"
  printf -v wave_tag "%02d" "$wave_num"
  task_file="$OUT_ROOT/tasks/task_wave_${wave_tag}.json"

  WAVE_SEND_START_MS="$(now_ms)"
  echo "[mission] sending wave ${wave_num}"
  send_args=(
    python3 scripts/send_task.py
    --transport tcp \
    --task "$task_file" \
    --host "$SCHED_HOST" \
    --port "$SCHED_PORT"
  )
  dispatch_time=""
  if [[ -n "$VIRTUAL_BASE_ISO" ]]; then
    dispatch_time="$(python3 - "$VIRTUAL_BASE_ISO" "$virtual_offset" <<'PY'
from datetime import timedelta
import sys
from mvs.common.models import parse_iso_time
base = parse_iso_time(sys.argv[1])
offset = float(sys.argv[2] or 0)
print((base + timedelta(seconds=offset)).isoformat())
PY
)"
    send_args+=(--dispatch-time "$dispatch_time")
  fi
  "${send_args[@]}"
  WAVE_SEND_END_MS="$(now_ms)"
  WAVE_SEND_TOTAL_MS=$((WAVE_SEND_TOTAL_MS + WAVE_SEND_END_MS - WAVE_SEND_START_MS))

  if [[ -n "$dispatch_time" ]]; then
    send_ts="$(python3 - "$dispatch_time" <<'PY'
import sys
from zoneinfo import ZoneInfo
from mvs.common.models import parse_iso_time
print(parse_iso_time(sys.argv[1]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"))
PY
)"
  else
    send_ts="$(date '+%Y-%m-%d %H:%M:%S')"
  fi
  {
    echo "wave=${wave_num} sent_at=${send_ts} launches=${count} fire_after=${start_after}s interval=${interval}s same_fire_time=${same_fire_time}"
  } >> "$TIMELINE_FILE"

  if [[ "$RENDER_SNAPSHOTS" == "1" ]]; then
      echo "[mission] waiting ${SNAPSHOT_DELAY}s before snapshot of wave ${wave_num}"
    SNAPSHOT_START_MS="$(now_ms)"
    sleep_for_mode "$SNAPSHOT_DELAY" "$SIMULATION_MODE" "$SETTLE_DELAY_SEC"
    capture_state \
      "$DASH_HOST" "$DASH_PORT" \
      "$OUT_ROOT/visuals/wave_0${wave_num}_state.json" \
      "$OUT_ROOT/visuals/wave_0${wave_num}_snapshot.png" \
      "$CAPTURE_TIMEOUT"
    SNAPSHOT_END_MS="$(now_ms)"
    SNAPSHOT_TOTAL_MS=$((SNAPSHOT_TOTAL_MS + SNAPSHOT_END_MS - SNAPSHOT_START_MS))
  else
    : > "$OUT_ROOT/visuals/wave_0${wave_num}_state.json"
  fi

  if [[ "$idx" -lt $((${#WAVES[@]} - 1)) ]]; then
    if [[ -n "$gap" ]]; then
      if [[ "$RENDER_SNAPSHOTS" == "1" ]]; then
        remaining_wait=$(( gap - SNAPSHOT_DELAY ))
      else
        remaining_wait=$gap
      fi
    else
      remaining_wait=0
    fi
    if [[ "$remaining_wait" -gt 0 ]]; then
      echo "[mission] waiting ${remaining_wait}s before next wave"
      SIM_WAIT_START_MS="$(now_ms)"
      sleep_for_mode "$remaining_wait" "$SIMULATION_MODE" "$SETTLE_DELAY_SEC"
      SIM_WAIT_END_MS="$(now_ms)"
      SIM_WAIT_TOTAL_MS=$((SIM_WAIT_TOTAL_MS + SIM_WAIT_END_MS - SIM_WAIT_START_MS))
    fi
  fi
done
WAVE_PHASE_END_MS="$(now_ms)"

COMPLETION_START_MS="$(now_ms)"
MISSION_PLAN_READY_SEC="-1"
MISSION_DISPATCH_READY_SEC="-1"
if [[ "$WAIT_UNTIL_ALL_DONE" == "1" ]]; then
  echo "[mission] waiting for all subtasks to finish (timeout ${COMPLETION_TIMEOUT_SEC}s, poll ${COMPLETION_POLL_SEC}s)"
  if wait_output="$(wait_for_completion "$DASH_HOST" "$DASH_PORT" "$COMPLETION_TIMEOUT_SEC" "$COMPLETION_POLL_SEC" 2>&1)"; then
    FINAL_WAIT_STATUS="$wait_output"
    echo "[mission] completion reached: $wait_output"
  else
    FINAL_WAIT_STATUS="$wait_output"
    echo "[mission] completion timeout: $wait_output"
    echo "[mission] falling back to final snapshot after configured wait ${WAIT_AFTER_FINAL_SEND}s"
    sleep_for_mode "$WAIT_AFTER_FINAL_SEND" "$SIMULATION_MODE" "$SETTLE_DELAY_SEC"
  fi
  if [[ "$FINAL_WAIT_STATUS" =~ mission_plan_ready_sec=([0-9.]+) ]]; then
    MISSION_PLAN_READY_SEC="${BASH_REMATCH[1]}"
  fi
  if [[ "$FINAL_WAIT_STATUS" =~ mission_dispatch_ready_sec=([0-9.]+) ]]; then
    MISSION_DISPATCH_READY_SEC="${BASH_REMATCH[1]}"
  fi
else
  echo "[mission] waiting ${WAIT_AFTER_FINAL_SEND}s after final wave"
  sleep_for_mode "$WAIT_AFTER_FINAL_SEND" "$SIMULATION_MODE" "$SETTLE_DELAY_SEC"
fi
COMPLETION_END_MS="$(now_ms)"

FINAL_CAPTURE_START_MS="$(now_ms)"
capture_state \
  "$DASH_HOST" "$DASH_PORT" \
  "$OUT_ROOT/visuals/final_state.json" \
  "$OUT_ROOT/visuals/final_snapshot.png" \
  "$CAPTURE_TIMEOUT"
FINAL_CAPTURE_END_MS="$(now_ms)"

EXPORT_PLANNING_START_MS="$(now_ms)"
python3 scripts/export_planning_results.py \
  --scheduler-log logs/scheduler_events.jsonl \
  --out "$OUT_ROOT/results/planning_results.json" >/dev/null
EXPORT_PLANNING_END_MS="$(now_ms)"

EXPORT_DISPATCH_METRICS_START_MS="$(now_ms)"
python3 scripts/export_wave_dispatch_metrics.py \
  --scheduler-log logs/scheduler_events.jsonl \
  --out-json "$OUT_ROOT/results/wave_dispatch_metrics.json" \
  --out-txt "$OUT_ROOT/results/leader_package/05_wave_dispatch_metrics.txt" >/dev/null
EXPORT_DISPATCH_METRICS_END_MS="$(now_ms)"

RESULT_READY_MS="$EXPORT_PLANNING_END_MS"

python3 - "$OUT_ROOT/visuals/final_state.json" "$TIMELINE_FILE" "$RUN_START_MS" "$SCHEDULING_START_MS" "$RESULT_READY_MS" \
  "$MISSION_PLAN_READY_SEC" "$MISSION_DISPATCH_READY_SEC" \
  "$BUILD_START_MS" "$BUILD_END_MS" \
  "$PREVIEW_START_MS" "$PREVIEW_END_MS" \
  "$MAP_ELEMENTS_START_MS" "$MAP_ELEMENTS_END_MS" \
  "$STOP_PREVIOUS_START_MS" "$STOP_PREVIOUS_END_MS" \
  "$STACK_START_MS" "$STACK_END_MS" \
  "$READY_START_MS" "$READY_END_MS" \
  "$WAVE_PHASE_START_MS" "$WAVE_PHASE_END_MS" "$WAVE_SEND_TOTAL_MS" "$SNAPSHOT_TOTAL_MS" "$SIM_WAIT_TOTAL_MS" \
  "$COMPLETION_START_MS" "$COMPLETION_END_MS" \
  "$FINAL_CAPTURE_START_MS" "$FINAL_CAPTURE_END_MS" \
  "$EXPORT_PLANNING_START_MS" "$EXPORT_PLANNING_END_MS" \
  "$EXPORT_DISPATCH_METRICS_START_MS" "$EXPORT_DISPATCH_METRICS_END_MS" <<'PY'
import json, sys, time
state_path, timeline_path = sys.argv[1], sys.argv[2]
run_start_ms, scheduling_start_ms, result_ready_ms = [int(x) for x in sys.argv[3:6]]
mission_plan_ready_sec = float(sys.argv[6])
mission_dispatch_ready_sec = float(sys.argv[7])
raw = [int(x) for x in sys.argv[8:]]
(
    build_start, build_end,
    preview_start, preview_end,
    map_start, map_end,
    stop_start, stop_end,
    stack_start, stack_end,
    ready_start, ready_end,
    wave_start, wave_end, wave_send_total, snapshot_total, sim_wait_total,
    completion_start, completion_end,
    final_capture_start, final_capture_end,
    export_planning_start, export_planning_end,
    export_dispatch_metrics_start, export_dispatch_metrics_end,
) = raw
last_error = None
for _ in range(10):
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        break
    except json.JSONDecodeError as exc:
        last_error = exc
        time.sleep(0.2)
else:
    raise last_error
metrics = state.get("metrics", {})
planning_ready_ms = result_ready_ms if mission_plan_ready_sec < 0 else int(completion_start + mission_plan_ready_sec * 1000.0)
dispatch_ready_ms = result_ready_ms if mission_dispatch_ready_sec < 0 else int(completion_start + mission_dispatch_ready_sec * 1000.0)
planning_ready_wall_sec = max(0.0, (planning_ready_ms - scheduling_start_ms) / 1000.0)
dispatch_ready_wall_sec = max(0.0, (dispatch_ready_ms - scheduling_start_ms) / 1000.0)
closed_loop_wall_sec = max(0.0, (result_ready_ms - scheduling_start_ms) / 1000.0)
full_wall_sec = max(0.0, (result_ready_ms - run_start_ms) / 1000.0)
def sec(start, end):
    return max(0.0, (end - start) / 1000.0)
stages = [
    ("build_region_demo", "SHP建图与配置生成", sec(build_start, build_end)),
    ("render_base_preview", "底图预览渲染", sec(preview_start, preview_end)),
    ("render_map_elements", "地图要素渲染", sec(map_start, map_end)),
    ("stop_previous_stack", "清理旧进程", sec(stop_start, stop_end)),
    ("start_stack", "启动调度与车辆进程", sec(stack_start, stack_end)),
    ("wait_ready", "等待调度面板与车辆心跳", sec(ready_start, ready_end)),
    ("wave_phase_total", "任务批次生成、发送、快照与虚拟间隔", sec(wave_start, wave_end)),
    ("wave_send_total", "任务文件生成与发送合计", max(0.0, wave_send_total / 1000.0)),
    ("wave_snapshot_total", "波次快照采集合计", max(0.0, snapshot_total / 1000.0)),
    ("simulated_gap_wait", "离散模式波次间隔等待开销", max(0.0, sim_wait_total / 1000.0)),
    ("completion_wait", "等待全部任务闭环", sec(completion_start, completion_end)),
    ("final_capture", "最终状态JSON采集", sec(final_capture_start, final_capture_end)),
    ("export_planning_results", "规划结果导出", sec(export_planning_start, export_planning_end)),
    ("export_wave_dispatch_metrics", "波次调度下发耗时导出", sec(export_dispatch_metrics_start, export_dispatch_metrics_end)),
]
with open(timeline_path, "a", encoding="utf-8") as f:
    f.write("\nFinal metrics\n")
    f.write(
        f"vehicles_online={metrics.get('vehicles_online', 0)} "
        f"mission_tasks={metrics.get('mission_tasks_total', metrics.get('subtasks_total', 0))} "
        f"redundant_tasks={metrics.get('redundant_tasks_total', 0)} "
        f"mission_pending={metrics.get('mission_tasks_pending', metrics.get('subtasks_pending', 0))} "
        f"mission_running={metrics.get('mission_tasks_running', metrics.get('subtasks_running', 0))} "
        f"redundant_pending={metrics.get('redundant_tasks_pending', 0)} "
        f"redundant_running={metrics.get('redundant_tasks_running', 0)}\n"
    )
    f.write(
        f"runtime_check planning_ready_wall_sec={planning_ready_wall_sec:.3f} "
        f"dispatch_ready_wall_sec={dispatch_ready_wall_sec:.3f} "
        f"closed_loop_wall_sec={closed_loop_wall_sec:.3f} "
        f"full_wall_sec={full_wall_sec:.3f} target_sec=10.000 "
        f"pass_10s={str(planning_ready_wall_sec <= 10.0).lower()}\n"
    )
    for key, label, value in stages:
        f.write(f"runtime_stage key={key} label={label} wall_sec={value:.3f}\n")
PY

{
  echo "completion_wait=$FINAL_WAIT_STATUS"
} >> "$TIMELINE_FILE"

EXPORT_LEADER_START_MS="$(now_ms)"
python3 scripts/export_leader_package.py \
  --run-root "$OUT_ROOT" \
  --logs-dir logs >/dev/null
EXPORT_LEADER_END_MS="$(now_ms)"
python3 - "$TIMELINE_FILE" "$EXPORT_LEADER_START_MS" "$EXPORT_LEADER_END_MS" <<'PY'
import sys
timeline_path = sys.argv[1]
start, end = int(sys.argv[2]), int(sys.argv[3])
with open(timeline_path, "a", encoding="utf-8") as f:
    f.write(f"runtime_stage key=export_leader_package label=领导简报与地图包导出 wall_sec={max(0.0, (end - start) / 1000.0):.3f}\n")
PY

echo
if [[ "$RENDER_BASE_PREVIEW" == "1" ]]; then
  echo "[mission] base preview   : $OUT_ROOT/visuals/base_preview.png"
else
  echo "[mission] base preview   : skipped"
fi
if [[ "$RENDER_MAP_ELEMENTS" == "1" ]]; then
  echo "[mission] map elements   : $OUT_ROOT/visuals/map_elements.png"
else
  echo "[mission] map elements   : skipped"
fi
echo "[mission] timeline       : $TIMELINE_FILE"
if [[ "$RENDER_SNAPSHOTS" == "1" ]]; then
  echo "[mission] wave snapshots : $OUT_ROOT/visuals/"
  echo "[mission] final snapshot : $OUT_ROOT/visuals/final_snapshot.png"
else
  echo "[mission] snapshots      : json only, png skipped"
fi
echo "[mission] planning data  : $OUT_ROOT/results/planning_results.json"
echo "[mission] dispatch metric: $OUT_ROOT/results/wave_dispatch_metrics.json"
echo "[mission] leader package : $OUT_ROOT/results/leader_package"
echo "[mission] dashboard      : http://$DASH_HOST:$DASH_PORT"

if [[ "$STOP_AT_END" == "1" ]]; then
  echo "[mission] stopping local stack"
  scripts/stack_down.sh \
    --scheduler-config "$OUT_ROOT/configs/scheduler_debug.json" \
    --vehicles-dir "$OUT_ROOT/configs/vehicles" >/dev/null 2>&1 || true
else
  echo "[mission] stack still running; stop with: scripts/stack_down.sh"
fi
