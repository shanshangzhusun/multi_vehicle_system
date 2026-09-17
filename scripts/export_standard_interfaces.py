#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def add_seconds(ts: Any, seconds: float) -> Optional[str]:
    dt = parse_ts(ts)
    if not dt:
        return None
    return (dt + timedelta(seconds=seconds)).isoformat()


def latest_task_prefix(rows: Iterable[Dict[str, Any]]) -> str:
    task_rows = [row for row in rows if row.get("event") == "task_received" and row.get("task_id")]
    if not task_rows:
        return ""
    latest = max(task_rows, key=lambda row: str(row.get("ts") or ""))
    task_id = str(latest.get("task_id") or "")
    parts = task_id.rsplit("_", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else task_id


def in_run(row: Dict[str, Any], prefix: str) -> bool:
    if not prefix:
        return True
    for key in ("task_id", "subtask_id", "plan_id"):
        if str(row.get(key) or "").startswith(prefix):
            return True
    return False


def plan_map(plans: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for plan in plans:
        sid = str(plan.get("subtask_id") or "")
        phase = str(plan.get("phase") or "")
        if sid and phase:
            out[sid][phase] = plan
    return out


def hide_arrival_time(to_fire: Dict[str, Any]) -> Optional[str]:
    wait_node_id = to_fire.get("wait_node_id") or (to_fire.get("path_report") or {}).get("hide_point")
    wait_node_index = to_fire.get("wait_node_index")
    edge_seconds = [float(x) for x in to_fire.get("edge_seconds", [])]
    if not wait_node_id or wait_node_index is None:
        return None
    try:
        edge_count = max(0, min(int(wait_node_index), len(edge_seconds)))
    except (TypeError, ValueError):
        return None
    return add_seconds(to_fire.get("start_at"), sum(edge_seconds[:edge_count]))


def build_stage_timeline(planning: Dict[str, Any], final_state: Dict[str, Any], prefix: str) -> List[Dict[str, Any]]:
    plans = [row for row in planning.get("plans", []) or [] if in_run(row, prefix)]
    by_sid = plan_map(plans)
    state_by_sid = {
        str(row.get("subtask_id") or ""): row
        for row in final_state.get("subtasks", []) or []
        if row.get("subtask_id") and in_run(row, prefix)
    }
    rows: List[Dict[str, Any]] = []
    for sid in sorted(set(by_sid) | set(state_by_sid)):
        state = state_by_sid.get(sid, {})
        phases = by_sid.get(sid, {})
        to_fire = phases.get("to_fire", {})
        to_depot = phases.get("to_depot", {})
        reload_plan = phases.get("reload_at_depot", {})
        return_plan = phases.get("return_home", {})
        path_report = to_fire.get("path_report") or {}
        fire_ready = to_fire.get("fire_ready_at") or to_fire.get("end_at")
        target_fire_time = (
            path_report.get("desired_fire_time")
            or state.get("fire_time")
            or to_fire.get("desired_fire_time")
        )
        hide_point_id = to_fire.get("wait_node_id") or path_report.get("hide_point")
        redundant = bool(state.get("redundant") or to_fire.get("redundant") or to_fire.get("suppress_fire"))
        rows.append(
            {
                "schema": "stage_timeline_v1",
                "task_id": state.get("task_id") or to_fire.get("task_id"),
                "subtask_id": sid,
                "vehicle_id": state.get("assigned_vehicle") or to_fire.get("vehicle_id"),
                "ammo_type": state.get("ammo_type") or "",
                "ammo_count": 0 if redundant else 1,
                "fire_point_id": state.get("assigned_launch_point") or to_fire.get("target_node"),
                "hide_point_id": hide_point_id or None,
                "depot_point_id": state.get("depot_node") or to_depot.get("target_node") or reload_plan.get("target_node"),
                "standby_point_id": return_plan.get("target_node"),
                "target_fire_time": target_fire_time,
                "planned_depart_time": to_fire.get("start_at"),
                "planned_hide_arrive_time": hide_arrival_time(to_fire),
                "planned_hide_wait_sec": float(to_fire.get("wait_seconds", 0.0) or 0.0),
                "planned_fire_arrive_time": to_fire.get("arrival_at") or to_fire.get("end_at"),
                "planned_fire_ready_time": fire_ready,
                "planned_fire_start_time": target_fire_time,
                "planned_fire_end_time": fire_ready,
                "planned_depot_arrive_time": to_depot.get("arrival_at") or to_depot.get("end_at"),
                "planned_reload_done_time": reload_plan.get("end_at"),
                "planned_standby_arrive_time": return_plan.get("arrival_at") or return_plan.get("end_at"),
                "status": "FINALIZED" if state.get("status") == "DONE" else (state.get("status") or "PLANNED"),
            }
        )
    return rows


def build_point_assignment(
    scheduler_rows: List[Dict[str, Any]],
    planning: Dict[str, Any],
    final_state: Dict[str, Any],
    prefix: str,
) -> List[Dict[str, Any]]:
    plans = [row for row in planning.get("plans", []) or [] if in_run(row, prefix)]
    phases_by_sid = plan_map(plans)
    assigned_rows = [
        row
        for row in scheduler_rows
        if row.get("event") in {"subtask_assigned", "subtask_reserved_after_reload"} and in_run(row, prefix)
    ]
    state_by_sid = {
        str(row.get("subtask_id") or ""): row
        for row in final_state.get("subtasks", []) or []
        if row.get("subtask_id")
    }
    rows: List[Dict[str, Any]] = []
    seen = set()
    for row in assigned_rows:
        sid = str(row.get("subtask_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        state = state_by_sid.get(sid, {})
        phases = phases_by_sid.get(sid, {})
        to_fire = phases.get("to_fire", {})
        to_depot = phases.get("to_depot", {})
        reload_plan = phases.get("reload_at_depot", {})
        fire_point = state.get("assigned_launch_point") or to_fire.get("target_node") or row.get("launch_point")
        hide_point = to_fire.get("wait_node_id") or (to_fire.get("path_report") or {}).get("hide_point")
        depot_point = state.get("depot_node") or to_depot.get("target_node") or reload_plan.get("target_node")
        reserve_windows = []
        if fire_point and (to_fire.get("arrival_at") or to_fire.get("start_at")):
            reserve_windows.append(
                {
                    "point_id": fire_point,
                    "point_type": "FIRE",
                    "start_time": to_fire.get("arrival_at") or to_fire.get("start_at"),
                    "end_time": to_fire.get("fire_ready_at") or to_fire.get("end_at"),
                    "exclusive": True,
                }
            )
        if depot_point and (to_depot.get("end_at") or reload_plan.get("start_at")):
            reserve_windows.append(
                {
                    "point_id": depot_point,
                    "point_type": "DEPOT",
                    "start_time": to_depot.get("end_at") or reload_plan.get("start_at"),
                    "end_time": reload_plan.get("end_at"),
                    "exclusive": True,
                }
            )
        rows.append(
            {
                "schema": "point_assignment_v1",
                "task_id": row.get("task_id") or state.get("task_id"),
                "subtask_id": sid,
                "vehicle_id": state.get("assigned_vehicle") or row.get("vehicle_id"),
                "fire_point_id": fire_point,
                "hide_point_id": hide_point or None,
                "depot_point_id": depot_point,
                "standby_point_id": (phases.get("return_home") or {}).get("target_node"),
                "reserve_windows": reserve_windows,
                "issued_at": row.get("ts"),
                "decision_score": state.get("score_breakdown") or row.get("score_breakdown") or {},
            }
        )
    return rows


def phase_for_vehicle(vehicle: Dict[str, Any]) -> str:
    active_plan = vehicle.get("active_plan") or {}
    phase = str(active_plan.get("phase") or vehicle.get("active_phase") or "")
    mapping = {
        "to_fire": "to_fire",
        "to_depot": "to_depot",
        "reload_at_depot": "reloading",
        "return_home": "to_standby",
    }
    if phase in mapping:
        return mapping[phase]
    status = str(vehicle.get("status") or "").upper()
    if "DEPOT" in status:
        return "to_depot"
    if "RELOAD" in status:
        return "reloading"
    if "RETURN" in status:
        return "to_standby"
    if "FIRE" in status:
        return "to_fire"
    return "idle"


def build_realtime_pose(final_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    map_obj = final_state.get("map") or {}
    node_by_id = {
        str(row.get("id") or ""): row
        for row in (final_state.get("nodes", []) or map_obj.get("nodes", []) or [])
        if row.get("id")
    }
    timestamp = final_state.get("server_time") or datetime.now().isoformat()
    rows: List[Dict[str, Any]] = []
    for seq, vehicle in enumerate(final_state.get("vehicles", []) or [], start=1):
        node_id = str(vehicle.get("current_node") or "")
        node = node_by_id.get(node_id, {})
        active_plan = vehicle.get("active_plan") or {}
        phase = phase_for_vehicle(vehicle)
        rows.append(
            {
                "schema": "realtime_pose_v1",
                "vehicle_id": vehicle.get("vehicle_id"),
                "seq": seq,
                "timestamp": timestamp,
                "task_id": active_plan.get("task_id"),
                "subtask_id": vehicle.get("active_subtask_id") or active_plan.get("subtask_id"),
                "phase": phase,
                "x": float(node.get("x", 0.0) or 0.0),
                "y": float(node.get("y", 0.0) or 0.0),
                "yaw_deg": 0.0,
                "speed_mps": 0.0 if phase == "idle" else float(vehicle.get("speed_mps", 0.0) or 0.0),
            }
        )
    return rows


def export_standard(
    scheduler_log: Path,
    planning_json: Path,
    final_state_json: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    scheduler_rows = read_jsonl(scheduler_log)
    prefix = latest_task_prefix(scheduler_rows)
    planning = read_json(planning_json, {})
    final_state = read_json(final_state_json, {})
    stage_timeline = build_stage_timeline(planning, final_state, prefix)
    point_assignment = build_point_assignment(scheduler_rows, planning, final_state, prefix)
    realtime_pose = build_realtime_pose(final_state)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stage_timeline.json").write_text(json.dumps(stage_timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "point_assignment.json").write_text(json.dumps(point_assignment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "realtime_pose.json").write_text(json.dumps(realtime_pose, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema": "mvs_standard_interface_manifest_v1",
        "run_prefix": prefix,
        "files": {
            "stage_timeline": str(out_dir / "stage_timeline.json"),
            "point_assignment": str(out_dir / "point_assignment.json"),
            "realtime_pose": str(out_dir / "realtime_pose.json"),
        },
        "counts": {
            "stage_timeline": len(stage_timeline),
            "point_assignment": len(point_assignment),
            "realtime_pose": len(realtime_pose),
        },
    }
    (out_dir / "standard_interface_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export standard stage/assignment/realtime interface JSON files.")
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl")
    parser.add_argument("--planning-json", default="result/results/planning_results.json")
    parser.add_argument("--final-state", default="result/visuals/final_state.json")
    parser.add_argument("--out-dir", default="result/results")
    args = parser.parse_args()
    export_standard(
        scheduler_log=Path(args.scheduler_log),
        planning_json=Path(args.planning_json),
        final_state_json=Path(args.final_state),
        out_dir=Path(args.out_dir),
    )


if __name__ == "__main__":
    main()
