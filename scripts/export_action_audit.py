#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_local(value: Any) -> str:
    dt = parse_ts(value) if not isinstance(value, datetime) else value
    if not dt:
        return "-"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_events(path: Path) -> List[Dict[str, Any]]:
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


def load_scheduler_cfg_from_final_state(final_state_json: Path) -> Dict[str, Any]:
    run_root = final_state_json.resolve().parents[1]
    cfg_path = run_root / "configs" / "scheduler_debug.json"
    return read_json(cfg_path, {})


def latest_task_prefix(events: Iterable[Dict[str, Any]]) -> str:
    task_rows = [row for row in events if row.get("event") == "task_received" and row.get("task_id")]
    if not task_rows:
        return ""
    latest = max(task_rows, key=lambda row: str(row.get("ts") or ""))
    task_id = str(latest.get("task_id") or "")
    if "_wave_" in task_id:
        return task_id.split("_wave_", 1)[0]
    parts = task_id.rsplit("_", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else task_id


def in_run(row: Dict[str, Any], prefix: str) -> bool:
    if not prefix:
        return True
    for key in ("task_id", "subtask_id", "plan_id"):
        value = str(row.get(key) or "")
        if value.startswith(prefix):
            return True
    return False


def phase_interval(plan: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    start = parse_ts(plan.get("start_at"))
    end = parse_ts(plan.get("end_at") or plan.get("arrival_at"))
    if not start or not end:
        return None
    return start, end


def wave_index(task_id: str) -> int:
    for token in ("_wave_", "_"):
        if token in task_id:
            tail = task_id.rsplit(token, 1)[-1]
            if tail.isdigit():
                return int(tail)
    return 0


def export_audit(
    scheduler_log: Path,
    depot_log: Path,
    planning_json: Path,
    final_state_json: Path,
    out_json: Path,
    out_txt: Path,
) -> Dict[str, Any]:
    scheduler_cfg = load_scheduler_cfg_from_final_state(final_state_json)
    prelaunch_only = bool(scheduler_cfg.get("prelaunch_only", False))
    events_all = read_events(scheduler_log)
    prefix = latest_task_prefix(events_all)
    events = [row for row in events_all if in_run(row, prefix)]
    event_counts = Counter(str(row.get("event") or "") for row in events)

    planning = read_json(planning_json, {})
    plans = [row for row in planning.get("plans", []) if in_run(row, prefix)]
    state = read_json(final_state_json, {})
    subtasks = [row for row in state.get("subtasks", []) if in_run(row, prefix)]
    depot_events_all = read_events(depot_log)
    depot_events = [row for row in depot_events_all if in_run(row, prefix)]
    depot_event_counts = Counter(str(row.get("event") or "") for row in depot_events)

    plan_by_sid: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for plan in plans:
        sid = str(plan.get("subtask_id") or "")
        phase = str(plan.get("phase") or "")
        if sid and phase:
            plan_by_sid[sid][phase].append(plan)

    event_by_sid: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in events:
        sid = str(row.get("subtask_id") or "")
        if sid:
            event_by_sid[sid][str(row.get("event") or "")].append(row)

    assigned_events = [row for row in events if row.get("event") == "subtask_assigned"]
    assigned_by_sid = {str(row.get("subtask_id") or ""): row for row in assigned_events if row.get("subtask_id")}

    subtasks_by_sid = {str(row.get("subtask_id") or ""): row for row in subtasks if row.get("subtask_id")}
    all_sids = sorted(set(subtasks_by_sid) | set(assigned_by_sid) | set(plan_by_sid))

    if prelaunch_only:
        required_real_phases = ["to_fire"]
        required_real_events = ["subtask_done"]
        required_redundant_phases = ["to_fire"]
        forbidden_redundant_phases = ["to_depot", "reload_at_depot", "return_home"]
        forbidden_redundant_events = ["request_depot", "depot_reload_start", "depot_reload_complete", "request_return"]
        forbidden_real_phases = ["to_depot", "reload_at_depot", "return_home"]
        forbidden_real_events = ["request_depot", "depot_reload_start", "depot_reload_complete", "request_return"]
    else:
        required_real_phases = ["to_fire", "to_depot", "reload_at_depot", "return_home"]
        required_real_events = ["request_depot", "depot_reload_start", "depot_reload_complete", "request_return", "subtask_done"]
        required_redundant_phases = ["to_fire", "return_home"]
        forbidden_redundant_phases = ["to_depot", "reload_at_depot"]
        forbidden_redundant_events = ["request_depot", "depot_reload_start", "depot_reload_complete"]
        forbidden_real_phases = []
        forbidden_real_events = []

    issues: List[str] = []
    subtask_audits: List[Dict[str, Any]] = []
    fire_errors: List[float] = []

    for sid in all_sids:
        st = subtasks_by_sid.get(sid, {})
        assigned = assigned_by_sid.get(sid, {})
        phase_map = plan_by_sid.get(sid, {})
        event_map = event_by_sid.get(sid, {})
        redundant = bool(st.get("redundant") or assigned.get("redundant"))
        task_id = str(st.get("task_id") or assigned.get("task_id") or "")
        vehicle_id = str(st.get("vehicle_id") or assigned.get("vehicle_id") or "-")
        launch_point = str(st.get("assigned_launch_point") or assigned.get("launch_point") or "-")
        status = str(st.get("status") or "-")

        missing_phases = [
            phase for phase in (required_redundant_phases if redundant else required_real_phases)
            if not phase_map.get(phase)
        ]
        missing_events = []
        if redundant:
            if not event_map.get("redundant_vehicle_ready"):
                missing_events.append("redundant_vehicle_ready")
            for phase in forbidden_redundant_phases:
                if phase_map.get(phase):
                    issues.append(f"{sid}: 冗余任务不应包含 {phase} 阶段")
            for event in forbidden_redundant_events:
                if event_map.get(event):
                    issues.append(f"{sid}: 冗余任务不应触发 {event} 事件")
        else:
            missing_events = [event for event in required_real_events if not event_map.get(event)]
            for phase in forbidden_real_phases:
                if phase_map.get(phase):
                    issues.append(f"{sid}: 当前射前模式不应包含 {phase} 阶段")
            for event in forbidden_real_events:
                if event_map.get(event):
                    issues.append(f"{sid}: 当前射前模式不应触发 {event} 事件")

        if missing_phases:
            issues.append(f"{sid}: 缺少阶段 {','.join(missing_phases)}")
        if missing_events:
            issues.append(f"{sid}: 缺少事件 {','.join(missing_events)}")
        if status != "DONE":
            issues.append(f"{sid}: 状态不是 DONE，而是 {status}")

        to_fire = phase_map.get("to_fire", [])
        fire_error = None
        if to_fire:
            fire_error = to_fire[-1].get("fire_time_error_sec")
            if isinstance(fire_error, (int, float)):
                fire_errors.append(float(fire_error))
                if abs(float(fire_error)) > 5.0 and not redundant:
                    issues.append(f"{sid}: 发射误差 {float(fire_error):.1f}s 超过 5s")

        subtask_audits.append(
            {
                "task_id": task_id,
                "subtask_id": sid,
                "vehicle_id": vehicle_id,
                "launch_point": launch_point,
                "redundant": redundant,
                "status": status,
                "phase_counts": {phase: len(rows) for phase, rows in sorted(phase_map.items())},
                "required_chain_ok": not missing_phases and not missing_events and status == "DONE",
                "fire_time_error_sec": fire_error,
            }
        )

    wave_audits: List[Dict[str, Any]] = []
    for task_id, rows in OrderedDict(
        (task, [row for row in subtask_audits if row["task_id"] == task])
        for task in sorted({row["task_id"] for row in subtask_audits}, key=wave_index)
    ).items():
        real = [row for row in rows if not row["redundant"]]
        redundant = [row for row in rows if row["redundant"]]
        real_launches = [row["launch_point"] for row in real]
        real_vehicles = [row["vehicle_id"] for row in real]
        duplicate_launch = sorted([key for key, count in Counter(real_launches).items() if key != "-" and count > 1])
        duplicate_vehicle = sorted([key for key, count in Counter(real_vehicles).items() if key != "-" and count > 1])
        if duplicate_launch:
            issues.append(f"{task_id}: 真实任务发射点重复 {duplicate_launch}")
        if duplicate_vehicle:
            issues.append(f"{task_id}: 真实任务车辆重复 {duplicate_vehicle}")
        wave_audits.append(
            {
                "task_id": task_id,
                "wave_index": wave_index(task_id),
                "real_count": len(real),
                "redundant_count": len(redundant),
                "real_done": sum(1 for row in real if row["status"] == "DONE"),
                "redundant_done": sum(1 for row in redundant if row["status"] == "DONE"),
                "unique_real_launch_points": len(set(real_launches)),
                "unique_real_vehicles": len(set(real_vehicles)),
                "duplicate_real_launch_points": duplicate_launch,
                "duplicate_real_vehicles": duplicate_vehicle,
            }
        )

    vehicle_intervals: Dict[str, List[Tuple[datetime, datetime, str, str]]] = defaultdict(list)
    for plan in plans:
        vehicle_id = str(plan.get("vehicle_id") or "")
        interval = phase_interval(plan)
        if not vehicle_id or not interval:
            continue
        vehicle_intervals[vehicle_id].append((interval[0], interval[1], str(plan.get("subtask_id") or ""), str(plan.get("phase") or "")))

    overlap_count = 0
    overlap_examples: List[Dict[str, str]] = []
    for vehicle_id, intervals in vehicle_intervals.items():
        intervals.sort(key=lambda item: item[0])
        for prev, cur in zip(intervals, intervals[1:]):
            if cur[0] < prev[1]:
                overlap_count += 1
                if len(overlap_examples) < 10:
                    overlap_examples.append(
                        {
                            "vehicle_id": vehicle_id,
                            "prev": f"{prev[2]}:{prev[3]} {prev[0].isoformat()}->{prev[1].isoformat()}",
                            "next": f"{cur[2]}:{cur[3]} {cur[0].isoformat()}->{cur[1].isoformat()}",
                        }
                    )
    if overlap_count:
        issues.append(f"车辆模拟时间段重叠 {overlap_count} 处")

    warnings = [
        row.get("detail")
        for row in events
        if row.get("event") == "task_validation_warning"
    ]

    summary = {
        "run_prefix": prefix,
        "subtasks_total": len(subtask_audits),
        "mission_tasks_total": sum(1 for row in subtask_audits if not row["redundant"]),
        "redundant_tasks_total": sum(1 for row in subtask_audits if row["redundant"]),
        "mission_done": sum(1 for row in subtask_audits if not row["redundant"] and row["status"] == "DONE"),
        "redundant_done": sum(1 for row in subtask_audits if row["redundant"] and row["status"] == "DONE"),
        "issues_count": len(issues),
        "vehicle_overlap_count": overlap_count,
        "task_validation_warning_count": len(warnings),
        "fire_error_abs_max_sec": round(max((abs(x) for x in fire_errors), default=0.0), 3),
        "fire_error_avg_abs_sec": round(sum(abs(x) for x in fire_errors) / len(fire_errors), 3) if fire_errors else 0.0,
        "event_counts": dict(event_counts),
        "depot_event_counts": dict(depot_event_counts),
        "depot_context_updates": int(depot_event_counts.get("depot_assignment_context_received", 0)),
        "depot_score_queries": int(depot_event_counts.get("depot_score_computed", 0)),
        "depot_assignment_results": int(depot_event_counts.get("depot_assignment_computed", 0)),
    }

    data = {
        "summary": summary,
        "issues": issues,
        "task_validation_warnings_sample": warnings[:20],
        "wave_audits": wave_audits,
        "subtask_audits": subtask_audits,
        "vehicle_overlap_examples": overlap_examples,
        "sources": {
            "scheduler_log": str(scheduler_log),
            "depot_log": str(depot_log),
            "planning_json": str(planning_json),
            "final_state_json": str(final_state_json),
            "scheduler_config": str(final_state_json.resolve().parents[1] / "configs" / "scheduler_debug.json"),
        },
        "depot_samples": {
            "context_updates": [row for row in depot_events if row.get("event") == "depot_assignment_context_received"][:10],
            "score_queries": [row for row in depot_events if row.get("event") == "depot_score_computed"][:20],
            "assignment_results": [row for row in depot_events if row.get("event") == "depot_assignment_computed"][:20],
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["车辆行动一致性审计", ""]
    lines.append(f"运行批次：{prefix or '-'}")
    lines.append(f"模式：{'射前闭环模式' if prelaunch_only else '含补给返航完整闭环模式'}")
    lines.append(
        "结论："
        + (
            "通过，任务链路与车辆行动在日志中闭环。"
            if not issues
            else f"发现 {len(issues)} 个需要复核的问题。"
        )
    )
    lines.append("")
    lines.append(
        f"总计 {summary['subtasks_total']} 个子任务，其中真实任务 {summary['mission_done']}/{summary['mission_tasks_total']} 完成，"
        f"冗余任务 {summary['redundant_done']}/{summary['redundant_tasks_total']} 完成。"
    )
    lines.append(
        f"车辆模拟时间重叠 {summary['vehicle_overlap_count']} 处；"
        f"真实发射最大误差 {summary['fire_error_abs_max_sec']}s，平均绝对误差 {summary['fire_error_avg_abs_sec']}s。"
    )
    lines.append(
        f"调度校验警告 {summary['task_validation_warning_count']} 条；这些警告来自上层任务包字段校验，不等同于最终点位重复。"
    )
    if prelaunch_only:
        lines.append("当前模式说明：本轮只审计射前机动、发射/冗余到位与子任务完成，不审计贮备库、装弹和返航阶段。")
    elif summary["depot_context_updates"] or summary["depot_score_queries"] or summary["depot_assignment_results"]:
        lines.append(
            f"独立贮备库模块：上下文更新 {summary['depot_context_updates']} 次，"
            f"评分计算 {summary['depot_score_queries']} 次，分配结果生成 {summary['depot_assignment_results']} 次。"
        )
    else:
        lines.append("独立贮备库模块：本轮未检测到 depot_events.jsonl 有效事件，说明当前结果未接入独立贮备库软件或该软件未产生日志。")
    lines.append("")
    lines.append("按波次检查：")
    for wave in wave_audits:
        lines.append(
            f"- 波次{wave['wave_index']}: 真实任务 {wave['real_done']}/{wave['real_count']} 完成，"
            f"冗余 {wave['redundant_done']}/{wave['redundant_count']} 完成；"
            f"真实发射点唯一 {wave['unique_real_launch_points']}/{wave['real_count']}，"
            f"真实车辆唯一 {wave['unique_real_vehicles']}/{wave['real_count']}。"
        )
    if issues:
        lines.append("")
        lines.append("需要复核的问题：")
        for item in issues[:30]:
            lines.append(f"- {item}")
        if len(issues) > 30:
            lines.append(f"- 其余 {len(issues) - 30} 条见 JSON。")
    lines.append("")
    if prelaunch_only:
        lines.append("审计依据：调度事件日志、车辆事件日志、已下发执行轨迹、Dashboard 最终状态四类数据交叉验证。")
    else:
        lines.append("审计依据：调度事件日志、独立贮备库日志、已下发执行轨迹、Dashboard 最终状态四类数据交叉验证。")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an auditable consistency report for vehicle actions.")
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl")
    parser.add_argument("--depot-log", default="logs/depot_events.jsonl")
    parser.add_argument("--planning-json", default="result/results/planning_results.json")
    parser.add_argument("--final-state", default="result/visuals/final_state.json")
    parser.add_argument("--out-json", default="result/results/action_audit.json")
    parser.add_argument("--out-txt", default="result/results/leader_package/06_action_audit.txt")
    args = parser.parse_args()
    export_audit(
        scheduler_log=Path(args.scheduler_log),
        depot_log=Path(args.depot_log),
        planning_json=Path(args.planning_json),
        final_state_json=Path(args.final_state),
        out_json=Path(args.out_json),
        out_txt=Path(args.out_txt),
    )


if __name__ == "__main__":
    main()
