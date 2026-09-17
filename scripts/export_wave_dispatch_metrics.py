#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_local(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def read_events(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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


def export_metrics(log_path: Path, out_json: Path, out_txt: Path) -> Dict[str, Any]:
    waves: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    rows = list(read_events(log_path))
    prefix = latest_task_prefix(rows)

    for row in rows:
        event = row.get("event")
        task_id_for_filter = str(row.get("task_id") or "")
        subtask_id_for_filter = str(row.get("subtask_id") or "")
        if prefix and not (task_id_for_filter.startswith(prefix) or subtask_id_for_filter.startswith(prefix)):
            continue
        if event == "task_received":
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            received_at = parse_ts(row.get("ts"))
            waves.setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "received_at": row.get("ts"),
                    "received_at_local": fmt_local(received_at),
                    "mission_expected": int(row.get("launches", 0) or 0),
                    "redundant_expected": int(row.get("redundant_launches", 0) or 0),
                    "mission_to_fire_plan_count": 0,
                    "redundant_to_fire_plan_count": 0,
                    "_received_dt": received_at,
                    "_mission_last_dt": None,
                    "_all_last_dt": None,
                },
            )
            continue

        if event != "plan_execute" or row.get("phase") != "to_fire":
            continue
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        metric = waves.setdefault(
            task_id,
            {
                "task_id": task_id,
                "received_at": "",
                "received_at_local": "",
                "mission_expected": 0,
                "redundant_expected": 0,
                "mission_to_fire_plan_count": 0,
                "redundant_to_fire_plan_count": 0,
                "_received_dt": None,
                "_mission_last_dt": None,
                "_all_last_dt": None,
            },
        )
        issued_dt = parse_ts(row.get("ts") or row.get("issued_at"))
        if not bool(row.get("redundant")):
            metric["mission_to_fire_plan_count"] += 1
            if issued_dt and (metric["_mission_last_dt"] is None or issued_dt > metric["_mission_last_dt"]):
                metric["_mission_last_dt"] = issued_dt
        else:
            metric["redundant_to_fire_plan_count"] += 1
        if issued_dt and (metric["_all_last_dt"] is None or issued_dt > metric["_all_last_dt"]):
            metric["_all_last_dt"] = issued_dt

    output_waves = []
    for index, metric in enumerate(waves.values(), start=1):
        received_dt = metric.pop("_received_dt", None)
        mission_last_dt = metric.pop("_mission_last_dt", None)
        all_last_dt = metric.pop("_all_last_dt", None)
        mission_expected = int(metric.get("mission_expected", 0) or 0)
        redundant_expected = int(metric.get("redundant_expected", 0) or 0)
        mission_count = int(metric.get("mission_to_fire_plan_count", 0) or 0)
        redundant_count = int(metric.get("redundant_to_fire_plan_count", 0) or 0)

        metric.update(
            {
                "wave_index": index,
                "mission_dispatch_complete_at": mission_last_dt.isoformat() if mission_last_dt else "",
                "mission_dispatch_complete_at_local": fmt_local(mission_last_dt),
                "all_dispatch_complete_at": all_last_dt.isoformat() if all_last_dt else "",
                "all_dispatch_complete_at_local": fmt_local(all_last_dt),
                "mission_dispatch_wall_sec": (
                    round((mission_last_dt - received_dt).total_seconds(), 3)
                    if mission_last_dt and received_dt
                    else None
                ),
                "all_dispatch_wall_sec": (
                    round((all_last_dt - received_dt).total_seconds(), 3)
                    if all_last_dt and received_dt
                    else None
                ),
                "mission_missing_count": max(0, mission_expected - mission_count),
                "redundant_missing_count": max(0, redundant_expected - redundant_count),
                "all_expected": mission_expected + redundant_expected,
                "all_to_fire_plan_count": mission_count + redundant_count,
            }
        )
        output_waves.append(metric)

    data = {
        "metric_name": "wave_dispatch_complete_time",
        "definition": (
            "For each wave, the timestamp when the scheduler has sent the last conflict-resolved "
            "to_fire EXECUTE_PLAN for that wave. mission_* counts only real launch subtasks; "
            "all_* includes redundant vehicle subtasks."
        ),
        "source_log": str(log_path),
        "waves": output_waves,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["波次调度下发耗时", ""]
    lines.append("定义：调度平台完成冲突消解后，向中转平台下发该波最后一条 to_fire 执行轨迹的时间。")
    lines.append("")
    for wave in output_waves:
        lines.append(
            "波次{wave_index} {task_id}: 真任务 {mission_to_fire_plan_count}/{mission_expected} "
            "完成下发，耗时 {mission_dispatch_wall_sec}s，完成时间 {mission_dispatch_complete_at_local}；"
            "含冗余 {all_to_fire_plan_count}/{all_expected} 完成下发，耗时 {all_dispatch_wall_sec}s，"
            "完成时间 {all_dispatch_complete_at_local}。".format(**wave)
        )
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Export per-wave scheduler dispatch completion metrics.")
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl")
    parser.add_argument("--out-json", default="result/results/wave_dispatch_metrics.json")
    parser.add_argument("--out-txt", default="result/results/leader_package/05_wave_dispatch_metrics.txt")
    args = parser.parse_args()

    export_metrics(
        log_path=Path(args.scheduler_log),
        out_json=Path(args.out_json),
        out_txt=Path(args.out_txt),
    )


if __name__ == "__main__":
    main()
