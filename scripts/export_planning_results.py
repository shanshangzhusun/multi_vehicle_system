#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def latest_task_prefix(rows: Iterable[Dict[str, Any]]) -> str:
    task_rows = [row for row in rows if row.get("event") == "task_received" and row.get("task_id")]
    if not task_rows:
        return ""
    latest = max(task_rows, key=lambda row: str(row.get("ts") or ""))
    task_id = str(latest.get("task_id") or "")
    if "_wave_" in task_id:
        return task_id.split("_wave_", 1)[0]
    parts = task_id.rsplit("_", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else task_id


def annotate_trajectory(
    trajectory: List[Dict[str, Any]],
    start_at: Optional[str],
    edge_seconds: List[float],
) -> List[Dict[str, Any]]:
    if not trajectory:
        return []

    start_dt = parse_iso(start_at) if start_at else None
    travel_sec = float(sum(float(x) for x in (edge_seconds or [])))
    if len(trajectory) == 1:
        pt = dict(trajectory[0])
        pt["seq"] = 0
        pt["offset_sec"] = 0.0
        if start_dt is not None:
            pt["timestamp"] = start_dt.isoformat()
        return [pt]

    seg_lengths: List[float] = []
    total_len = 0.0
    for i in range(len(trajectory) - 1):
        a = trajectory[i]
        b = trajectory[i + 1]
        seg = math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"]))
        seg_lengths.append(seg)
        total_len += seg

    out: List[Dict[str, Any]] = []
    accum = 0.0
    for idx, pt in enumerate(trajectory):
        row = dict(pt)
        row["seq"] = idx
        if idx > 0:
            accum += seg_lengths[idx - 1]
        if total_len > 1e-6:
            offset_sec = travel_sec * (accum / total_len)
        else:
            offset_sec = 0.0
        row["offset_sec"] = round(offset_sec, 3)
        if start_dt is not None:
            row["timestamp"] = (start_dt + timedelta(seconds=offset_sec)).isoformat()
        out.append(row)
    return out


def normalize_trajectory_geo(trajectory_geo: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, pt in enumerate(trajectory_geo or []):
        row: Dict[str, Any] = {"seq": idx}
        if "lon" in pt:
            row["lon"] = pt.get("lon")
        if "lat" in pt:
            row["lat"] = pt.get("lat")
        if "time" in pt:
            row["timestamp"] = pt.get("time")
        elif "timestamp" in pt:
            row["timestamp"] = pt.get("timestamp")
        out.append(row)
    return out


def export_results(
    scheduler_log: Path,
    depot_log: Path,
    out_path: Path,
    task_id: Optional[str],
    subtask_id: Optional[str],
    vehicle_id: Optional[str],
    phase: Optional[str],
) -> None:
    plans: List[Dict[str, Any]] = []
    seen = set()
    rows = list(load_jsonl(scheduler_log))
    latest_prefix = latest_task_prefix(rows) if not task_id else ""
    for row in rows:
        if row.get("event") != "plan_execute":
            continue
        if latest_prefix:
            row_task_id = str(row.get("task_id") or "")
            row_subtask_id = str(row.get("subtask_id") or "")
            if not (row_task_id.startswith(latest_prefix) or row_subtask_id.startswith(latest_prefix)):
                continue
        if task_id and row.get("task_id") != task_id:
            continue
        if subtask_id and row.get("subtask_id") != subtask_id:
            continue
        if vehicle_id and row.get("vehicle_id") != vehicle_id:
            continue
        if phase and row.get("phase") != phase:
            continue

        plan_key = (
            row.get("plan_id"),
            row.get("task_id"),
            row.get("subtask_id"),
            row.get("vehicle_id"),
            row.get("phase"),
        )
        if plan_key in seen:
            continue
        seen.add(plan_key)

        edge_seconds = [float(x) for x in row.get("edge_seconds", [])]
        wait_seconds = float(row.get("wait_seconds", 0.0) or 0.0)
        launch_startup_sec = float(row.get("launch_startup_sec", 0.0) or 0.0)
        path_report = row.get("path_report", {}) or {}
        start_at = row.get("start_at")
        end_at = row.get("end_at")
        travel_seconds = round(sum(edge_seconds), 3)
        arrival_at = None
        fire_ready_at = None
        if start_at:
            arrival_at = (parse_iso(start_at) + timedelta(seconds=travel_seconds + wait_seconds)).isoformat()
            fire_ready_at = (parse_iso(start_at) + timedelta(seconds=travel_seconds + wait_seconds + launch_startup_sec)).isoformat()

        trajectory_geo = normalize_trajectory_geo(row.get("trajectory_geo", []))
        plans.append(
            {
                "plan_id": row.get("plan_id"),
                "task_id": row.get("task_id"),
                "subtask_id": row.get("subtask_id"),
                "vehicle_id": row.get("vehicle_id"),
                "phase": row.get("phase"),
                "issued_at": row.get("issued_at"),
                "delay_sec": float(row.get("delay_sec", 0.0) or 0.0),
                "start_at": start_at,
                "arrival_at": arrival_at,
                "end_at": end_at,
                "target_node": row.get("target_node"),
                "node_path": row.get("node_path", []),
                "edge_seconds": edge_seconds,
                "travel_seconds": travel_seconds,
                "wait_seconds": wait_seconds,
                "wait_node_index": row.get("wait_node_index"),
                "wait_node_id": row.get("wait_node_id"),
                "launch_startup_sec": launch_startup_sec,
                "launch_startup_mode": row.get("launch_startup_mode"),
                "desired_fire_time": path_report.get("desired_fire_time"),
                "fire_time_error_sec": path_report.get("fire_time_error_sec"),
                "timing_strategy": path_report.get("timing_strategy"),
                "fire_ready_at": fire_ready_at,
                "path_report": path_report,
                "trajectory_points": (
                    trajectory_geo
                    if trajectory_geo
                    else annotate_trajectory(row.get("trajectory", []), start_at, edge_seconds)
                ),
            }
        )

    plans.sort(key=lambda x: (x.get("task_id") or "", x.get("subtask_id") or "", x.get("phase") or ""))
    depot_events = [row for row in load_jsonl(depot_log) if not latest_prefix or str(row.get("task_id") or str(row.get("subtask_id") or "")).startswith(latest_prefix)]
    depot_runtime = {
        "source_log": str(depot_log),
        "event_counts": dict(Counter(str(row.get("event") or "") for row in depot_events)),
        "score_queries": [
            {
                "ts": row.get("ts"),
                "task_id": row.get("task_id"),
                "subtask_id": row.get("subtask_id"),
                "vehicle_id": row.get("vehicle_id"),
                "launch_node": row.get("launch_node"),
                "ammo_type": row.get("ammo_type"),
                "selected_depot": row.get("selected_depot"),
                "depot_candidates": row.get("depot_candidates"),
            }
            for row in depot_events
            if row.get("event") == "depot_score_computed"
        ],
        "assignment_results": [
            {
                "ts": row.get("ts"),
                "task_id": row.get("task_id"),
                "subtask_id": row.get("subtask_id"),
                "vehicle_id": row.get("vehicle_id"),
                "launch_node": row.get("launch_node"),
                "ammo_type": row.get("ammo_type"),
                "selected_depot": row.get("selected_depot"),
                "depot_candidates": row.get("depot_candidates"),
            }
            for row in depot_events
            if row.get("event") == "depot_assignment_computed"
        ],
        "context_updates": [
            {
                "ts": row.get("ts"),
                "task_id": row.get("task_id"),
                "wave_id": row.get("wave_id"),
                "assignments": row.get("assignments"),
            }
            for row in depot_events
            if row.get("event") == "depot_assignment_context_received"
        ],
    }

    result = {
        "source_log": str(scheduler_log),
        "filters": {
            "task_id": task_id,
            "subtask_id": subtask_id,
            "vehicle_id": vehicle_id,
            "phase": phase,
            "latest_task_prefix": latest_prefix,
        },
        "plan_count": len(plans),
        "plans": plans,
        "depot_runtime": depot_runtime,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export scheduler planning results to JSON.")
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl", help="Scheduler JSONL event log path")
    parser.add_argument("--depot-log", default="logs/depot_events.jsonl", help="Depot JSONL event log path")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--task-id", help="Optional task_id filter")
    parser.add_argument("--subtask-id", help="Optional subtask_id filter")
    parser.add_argument("--vehicle-id", help="Optional vehicle_id filter")
    parser.add_argument("--phase", help="Optional phase filter, e.g. to_fire / to_depot / return_home")
    args = parser.parse_args()

    export_results(
        scheduler_log=Path(args.scheduler_log),
        depot_log=Path(args.depot_log),
        out_path=Path(args.out),
        task_id=args.task_id,
        subtask_id=args.subtask_id,
        vehicle_id=args.vehicle_id,
        phase=args.phase,
    )
    print(args.out)


if __name__ == "__main__":
    main()
