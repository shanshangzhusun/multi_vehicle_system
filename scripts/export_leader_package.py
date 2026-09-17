#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from mvs.scheduler.map_model import MapLoader


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    last_error: Optional[json.JSONDecodeError] = None
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error:
        raise last_error
    return {}


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def filter_rows_for_final_state(rows: List[Dict[str, Any]], final_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    subtasks = final_state.get("subtasks", []) or []
    valid_sids = {str(row.get("subtask_id") or "") for row in subtasks if row.get("subtask_id")}
    valid_tasks = {str(row.get("task_id") or "") for row in subtasks if row.get("task_id")}
    if not valid_sids and not valid_tasks:
        return rows
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        sid = str(row.get("subtask_id") or "")
        tid = str(row.get("task_id") or "")
        event = row.get("event")
        if sid and sid in valid_sids:
            filtered.append(row)
        elif tid and tid in valid_tasks:
            filtered.append(row)
        elif event == "scheduler_metrics":
            filtered.append(row)
    return filtered


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(ts: Optional[str]) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "-"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fmt_hm(ts: Optional[str]) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "-"
    return dt.astimezone(LOCAL_TZ).strftime("%H:%M:%S")


def seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    a = parse_ts(start)
    b = parse_ts(end)
    if not a or not b:
        return None
    return (b - a).total_seconds()


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}秒"
    return f"{seconds / 60.0:.1f}分钟"


def add_seconds(ts: Optional[str], seconds: float) -> Optional[str]:
    dt = parse_ts(ts)
    if not dt:
        return None
    return (dt + timedelta(seconds=seconds)).isoformat()


def project_factory(bounds: Tuple[float, float, float, float], width: int, height: int, padding: int = 40):
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1.0)
    dy = max(maxy - miny, 1.0)
    scale = min((width - 2 * padding) / dx, (height - 2 * padding) / dy)
    xoff = padding + (width - 2 * padding - dx * scale) / 2.0
    yoff = padding + (height - 2 * padding - dy * scale) / 2.0

    def project(x: float, y: float) -> Tuple[int, int]:
        px = xoff + (x - minx) * scale
        py = height - (yoff + (y - miny) * scale)
        return int(round(px)), int(round(py))

    return project


def graph_bounds(graph) -> Tuple[float, float, float, float]:
    xs = [n.x for n in graph.nodes.values()]
    ys = [n.y for n in graph.nodes.values()]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_to_canvas(project, bbox: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
    minx, miny, maxx, maxy = bbox
    p1 = project(minx, miny)
    p2 = project(maxx, maxy)
    left = min(p1[0], p2[0])
    right = max(p1[0], p2[0])
    top = min(p1[1], p2[1])
    bottom = max(p1[1], p2[1])
    return left, top, right, bottom


def draw_diamond(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: str, outline: str) -> None:
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill, outline=outline)


def render_map_overview(scheduler_cfg: Dict[str, Any], out_path: Path) -> None:
    graph = MapLoader.load_graph_from_config(scheduler_cfg["map"])
    points = MapLoader.load_points_from_config(scheduler_cfg["map"], graph)
    homes = [v["home_node"] for v in scheduler_cfg.get("vehicles", []) if v.get("home_node") in graph.nodes]
    prelaunch_only = bool(scheduler_cfg.get("prelaunch_only", False))

    width = 1600
    height = 1100
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    project = project_factory(graph_bounds(graph), width, height)
    fire_zone = scheduler_cfg.get("fire_zone") or {}
    fire_bbox = fire_zone.get("bbox")

    if fire_zone.get("enabled") and isinstance(fire_bbox, list) and len(fire_bbox) == 4:
        l, t, r, b = bbox_to_canvas(project, tuple(float(v) for v in fire_bbox))
        draw.rectangle((l, t, r, b), fill="#fee2e266", outline="#dc2626")

    for meta in graph.edge_meta.values():
        geom = meta.geometry or []
        if not geom:
            a = graph.nodes.get(meta.src)
            b = graph.nodes.get(meta.dst)
            if not a or not b:
                continue
            geom = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
        pts = [project(float(p["x"]), float(p["y"])) for p in geom]
        if len(pts) >= 2:
            draw.line(pts, fill="#dbe4ee", width=1, joint="curve")

    for node_id in homes:
        n = graph.nodes[node_id]
        px, py = project(n.x, n.y)
        draw_diamond(draw, px, py, 7, "#2563eb", "#0f172a")

    for point_id in points.hide_points:
        n = graph.nodes.get(point_id)
        if not n:
            continue
        px, py = project(n.x, n.y)
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill="#16a34a", outline="#14532d")

    for point_id in points.launch_points:
        n = graph.nodes.get(point_id)
        if not n:
            continue
        px, py = project(n.x, n.y)
        draw.rectangle((px - 6, py - 6, px + 6, py + 6), fill="#ef4444", outline="#7f1d1d")

    if not prelaunch_only:
        for point_id in points.depots:
            n = graph.nodes.get(point_id)
            if not n:
                continue
            px, py = project(n.x, n.y)
            draw.ellipse((px - 7, py - 7, px + 7, py + 7), fill="#f59e0b", outline="#78350f")

    draw.rectangle((12, 12, width - 12, 104), fill="#ffffff", outline="#e2e8f0")
    draw.text((24, 22), "Leader Map Overview", fill="#0f172a")
    draw.text(
        (24, 48),
        (
            f"roads={len(graph.edge_meta)}  nodes={len(graph.nodes)}  "
            f"vehicle_homes={len(homes)}  hide={len(points.hide_points)}  "
            f"launch={len(points.launch_points)}  "
            f"{f'depots={len(points.depots)}  ' if not prelaunch_only else ''}"
            f"fire_zone={'on' if fire_zone.get('enabled') else 'off'}"
        ),
        fill="#475569",
    )
    legend_y = 74
    draw.text((24, legend_y), "Legend:", fill="#475569")
    x = 88
    draw_diamond(draw, x, legend_y + 8, 6, "#2563eb", "#0f172a")
    draw.text((x + 12, legend_y), "vehicle homes", fill="#0f172a")
    x += 160
    draw.ellipse((x, legend_y + 2, x + 10, legend_y + 12), fill="#16a34a", outline="#14532d")
    draw.text((x + 18, legend_y), "hide points", fill="#0f172a")
    x += 150
    draw.rectangle((x, legend_y + 2, x + 12, legend_y + 14), fill="#ef4444", outline="#7f1d1d")
    draw.text((x + 18, legend_y), "launch points", fill="#0f172a")
    if not prelaunch_only:
        x += 165
        draw.ellipse((x, legend_y + 1, x + 14, legend_y + 15), fill="#f59e0b", outline="#78350f")
        draw.text((x + 22, legend_y), "depots", fill="#0f172a")
        x += 130
    draw.rectangle((x, legend_y + 2, x + 16, legend_y + 14), fill="#fee2e2", outline="#dc2626")
    draw.text((x + 24, legend_y), "fire zone", fill="#0f172a")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def load_wave_timeline(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if (
            raw.startswith("wave=")
            or raw.startswith("completion_wait=")
            or raw.startswith("runtime_check")
            or raw.startswith("runtime_stage")
        ):
            lines.append(raw)
    return lines


def parse_wave_line(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    last_key = ""
    for part in line.split():
        if "=" not in part:
            if last_key and last_key in out:
                out[last_key] = f"{out[last_key]} {part}".strip()
            continue
        k, v = part.split("=", 1)
        out[k] = v
        last_key = k
    return out


def final_subtask_state_map(final_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in final_state.get("subtasks", []) or []:
        sid = str(row.get("subtask_id") or "")
        if sid:
            out[sid] = row
    return out


def summarize_subtask_lifecycles(
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
    depot_rows: List[Dict[str, Any]],
    final_state: Dict[str, Any],
    *,
    prelaunch_only: bool = False,
) -> str:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in scheduler_rows:
        sid = str(row.get("subtask_id") or "")
        if sid:
            grouped[sid].append(row)
    for row in vehicle_rows:
        sid = str(row.get("subtask_id") or "")
        if sid:
            grouped[sid].append(row)
    depot_assignment_by_sid: Dict[str, Dict[str, Any]] = {}
    for row in depot_rows:
        if row.get("event") != "depot_assignment_computed":
            continue
        sid = str(row.get("subtask_id") or "")
        if sid:
            depot_assignment_by_sid[sid] = row

    state_map = final_subtask_state_map(final_state)
    lines: List[str] = []
    lines.append("子任务闭环明细")
    lines.append(f"生成时间：{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("说明")
    if prelaunch_only:
        lines.append("- 每个子任务按“分配 -> 射前机动 -> 发射/冗余到位 -> 完成”的顺序总结。")
        lines.append("- 当前模式只覆盖射前闭环，不包含贮备库、装弹和返航阶段。")
    else:
        lines.append("- 每个子任务按“分配 -> 发射 -> 去贮备库 -> 装弹 -> 返航/转后续任务”的顺序总结。")
        lines.append("- 如某个子任务没有完整闭环，会明确写出停留阶段，便于领导快速判断执行覆盖情况。")
    lines.append("")

    for sid in sorted(grouped):
        rows = sorted(
            grouped[sid],
            key=lambda r: (
                parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=LOCAL_TZ),
                str(r.get("event") or ""),
            ),
        )
        scheduler_only = [r for r in rows if r.get("event") != "vehicle_event"]
        vehicle_only = [r for r in rows if r.get("event") == "vehicle_event"]
        sub_state = state_map.get(sid, {})
        task_id = str(sub_state.get("task_id") or (sid.rsplit("_s", 1)[0] if "_s" in sid else "-"))
        assigned_events = [r for r in scheduler_only if r.get("event") == "subtask_assigned"]
        reserved_events = [r for r in scheduler_only if r.get("event") == "subtask_reserved_after_reload"]
        final_vehicle = str(sub_state.get("assigned_vehicle") or "")
        final_launch = str(sub_state.get("assigned_launch_point") or "")
        assigned = next(
            (
                r
                for r in reversed(assigned_events)
                if (not final_vehicle or r.get("vehicle_id") == final_vehicle)
                and (not final_launch or r.get("launch_point") == final_launch)
            ),
            assigned_events[-1] if assigned_events else {},
        )
        reserved = next(
            (
                r
                for r in reversed(reserved_events)
                if (not final_vehicle or r.get("vehicle_id") == final_vehicle)
                and (not final_launch or r.get("launch_point") == final_launch)
            ),
            reserved_events[-1] if reserved_events else {},
        )
        vehicle_id = str(final_vehicle or assigned.get("vehicle_id") or reserved.get("vehicle_id") or "-")
        launch = str(final_launch or assigned.get("launch_point") or reserved.get("launch_point") or "-")
        fire_time = fmt_hm(sub_state.get("fire_time") or assigned.get("fire_time") or reserved.get("fire_time"))
        final_status = str(sub_state.get("status") or "-")
        fire_plans = [r for r in scheduler_only if r.get("event") == "plan_execute" and r.get("phase") == "to_fire"]
        fire_plan = next(
            (
                r
                for r in reversed(fire_plans)
                if (not final_vehicle or r.get("vehicle_id") == final_vehicle)
                and (not final_launch or r.get("target_node") == final_launch)
            ),
            fire_plans[-1] if fire_plans else {},
        )
        hide_point = str(fire_plan.get("wait_node_id") or fire_plan.get("path_report", {}).get("hide_point") or "-")
        hide_selected = bool(fire_plan.get("path_report", {}).get("hide_selected")) or hide_point not in {"", "-", "None", "null"}
        startup_mode = str(fire_plan.get("launch_startup_mode") or fire_plan.get("path_report", {}).get("launch_startup_mode") or "-")
        startup_sec = float(fire_plan.get("launch_startup_sec", 0.0) or fire_plan.get("path_report", {}).get("launch_startup_sec", 0.0) or 0.0)

        phases_done: List[str] = []
        if any(r.get("event") == "plan_execute" and r.get("phase") == "to_fire" for r in scheduler_only):
            phases_done.append("已下发去发射点")
        if any(r.get("event") == "vehicle_event" and r.get("event_name") == "FIRED" for r in vehicle_only):
            phases_done.append("已发射")
        if not prelaunch_only:
            if any(r.get("event") == "plan_execute" and r.get("phase") == "to_depot" for r in scheduler_only):
                phases_done.append("已下发去贮备库")
            if any(r.get("event") == "depot_queue_join" for r in scheduler_only):
                phases_done.append("出现贮备库排队")
            if any(r.get("event") == "depot_reload_start" for r in scheduler_only):
                phases_done.append("已开始装弹")
            if any(r.get("event") == "depot_reload_complete" for r in scheduler_only):
                phases_done.append("已完成装弹")
            if any(r.get("event") == "plan_execute" and r.get("phase") == "return_home" for r in scheduler_only):
                phases_done.append("已下发返航")
        if any(r.get("event") == "subtask_done" for r in scheduler_only):
            phases_done.append("已完成闭环")

        lifecycle_parts: List[str] = []
        assign_mode = str(assigned.get("assignment_mode") or reserved.get("assignment_mode") or "idle")
        if assigned or reserved:
            lifecycle_parts.append(
                f"分配给 {vehicle_id}，目标发射点 {launch}，计划发射时间 {fire_time}，接单方式 {assign_mode}"
            )
        if fire_plan:
            lifecycle_parts.append(
                f"系统已下发去发射点轨迹，预计行驶 {human_duration(seconds_between(fire_plan.get('start_at'), fire_plan.get('end_at')))}"
            )
            if hide_selected:
                lifecycle_parts.append(f"途中先前往隐蔽点 {hide_point} 等待暴露窗口过去")
            if startup_mode and startup_mode != "-":
                lifecycle_parts.append(f"到达发射点后执行 {startup_mode} 启动，预计 {human_duration(startup_sec)}")
        fire_evt = next((r for r in vehicle_only if r.get("event_name") == "FIRED"), {})
        if fire_evt:
            lifecycle_parts.append(f"车辆在 {fire_evt.get('current_node')} 完成发射")
        if not prelaunch_only:
            depot_evt = next((r for r in scheduler_only if r.get("event") == "request_depot"), {})
            if depot_evt:
                lifecycle_parts.append(f"随后转向贮备库 {depot_evt.get('depot')}")
            queue_evt = next((r for r in scheduler_only if r.get("event") == "depot_queue_join"), {})
            if queue_evt:
                lifecycle_parts.append(f"到库后排队，前方序号 {queue_evt.get('queue_position')}")
            if any(r.get("event") == "depot_reload_complete" for r in scheduler_only):
                lifecycle_parts.append("完成装弹")
            depot_assignment = depot_assignment_by_sid.get(sid, {})
            if depot_assignment:
                lifecycle_parts.append(
                    f"独立贮备库模块推荐 {depot_assignment.get('selected_depot')}，候选库 {depot_assignment.get('depot_candidates')} 个"
                )
        if any(r.get("event") == "subtask_done" for r in scheduler_only):
            lifecycle_parts.append("最终完成冗余到位闭环" if bool(sub_state.get("redundant") or assigned.get("redundant")) else ("最终完成发射闭环" if prelaunch_only else "最后完成返航并恢复待命"))
        elif final_status and final_status != "DONE":
            lifecycle_parts.append(f"截至采集结束停留在 {final_status} 状态")

        special_notes: List[str] = []
        if reserved:
            special_notes.append(
                f"该任务不是直接派给空闲车，而是预留给 {vehicle_id}，等待其当前装弹完成后接续执行"
            )
        for row in scheduler_only:
            evt = row.get("event")
            if evt == "reservation_deadlock_risk":
                special_notes.append(
                    f"{'射前机动' if prelaunch_only else '返航'}阶段出现一次占道冲突退避，发生时间 {fmt_hm(row.get('ts'))}"
                )
            elif evt == "path_rejected":
                special_notes.append(f"路径被拒绝，原因 {row.get('reason')}")
            elif evt == "path_request_retry":
                special_notes.append(f"发生路径重试，第 {row.get('retry')} 次")
            elif evt == "direct_dispatch_after_reload" and not prelaunch_only:
                special_notes.append(
                    f"完成当前装弹后未返航，直接转入下一任务 {row.get('next_subtask_id')}"
                )
        if any(r.get("event") == "subtask_done" for r in scheduler_only) and final_status != "DONE":
            special_notes.append(
                f"事件链显示该任务已闭环完成，但最终状态快照仍为 {final_status}，说明采集时存在状态同步残留"
            )

        lines.append(sid)
        lines.append(f"- 所属波次：{task_id}")
        lines.append(f"- 最终状态：{final_status}")
        lines.append(f"- 隐蔽点策略：{'已启用，等待点 ' + hide_point if hide_selected else '未使用隐蔽点'}")
        lines.append(f"- 发射启动：{startup_mode if startup_mode and startup_mode != '-' else '未记录'}，启动时长 {human_duration(startup_sec) if startup_sec > 0 else '-'}")
        lines.append(f"- 阶段概览：{'、'.join(phases_done) if phases_done else '未进入有效执行阶段'}")
        lines.append(f"- 闭环描述：{'；'.join(lifecycle_parts) if lifecycle_parts else '未抓到有效生命周期信息'}。")
        lines.append(f"- 特殊情况：{'；'.join(special_notes) if special_notes else '无明显异常'}。")
        lines.append("")

    missing = sorted(set(state_map) - set(grouped))
    if missing:
        lines.append("补充")
        lines.append(f"- 以下子任务在日志中未找到有效事件链：{', '.join(missing)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_task_summary_lines(
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
    depot_rows: List[Dict[str, Any]],
    *,
    prelaunch_only: bool = False,
) -> List[str]:
    assignments: Dict[str, Dict[str, Any]] = {}
    plans_by_sid: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    vehicle_events_by_sid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in scheduler_rows:
        sid = str(row.get("subtask_id") or "")
        if not sid:
            continue
        evt = row.get("event")
        if evt == "subtask_assigned":
            assignments[sid] = row
        elif evt == "plan_execute":
            phase = str(row.get("phase") or "")
            if phase:
                plans_by_sid[sid][phase] = row
    for row in vehicle_rows:
        sid = str(row.get("subtask_id") or "")
        if sid and row.get("event") == "vehicle_event":
            vehicle_events_by_sid[sid].append(row)
    depot_assignments_by_sid: Dict[str, Dict[str, Any]] = {}
    for row in depot_rows:
        if row.get("event") != "depot_assignment_computed":
            continue
        sid = str(row.get("subtask_id") or "")
        if sid:
            depot_assignments_by_sid[sid] = row

    ordered_sids = sorted(
        set(assignments) | set(plans_by_sid) | set(vehicle_events_by_sid),
        key=lambda sid: (
            parse_ts((assignments.get(sid) or {}).get("fire_time")) or datetime.max.replace(tzinfo=LOCAL_TZ),
            sid,
        ),
    )

    lines: List[str] = []
    if not ordered_sids:
        lines.append("- 暂未抓到可汇总的任务执行结果。")
        return lines

    mission_idx = 0
    redundant_idx = 0
    for sid in ordered_sids:
        assigned = assignments.get(sid, {})
        plans = plans_by_sid.get(sid, {})
        fire_plan = plans.get("to_fire", {})
        to_depot = plans.get("to_depot", {})
        reload_plan = plans.get("reload_at_depot", {})
        return_plan = plans.get("return_home", {})
        vehicle_id = str(
            assigned.get("vehicle_id")
            or fire_plan.get("vehicle_id")
            or to_depot.get("vehicle_id")
            or "-"
        )
        launch_node = str(assigned.get("launch_point") or fire_plan.get("target_node") or "-")
        fire_time = assigned.get("fire_time") or fire_plan.get("path_report", {}).get("desired_fire_time")
        wait_seconds = float(fire_plan.get("wait_seconds", 0.0) or 0.0)
        wait_node_id = fire_plan.get("wait_node_id") or fire_plan.get("path_report", {}).get("hide_point")
        wait_node_id = str(wait_node_id) if wait_node_id else ""
        wait_node_index = fire_plan.get("wait_node_index")
        edge_seconds = [float(x) for x in fire_plan.get("edge_seconds", [])]
        start_at = fire_plan.get("start_at")
        hide_arrival_at = None
        if wait_node_id and wait_node_index is not None:
            try:
                edge_count = max(0, min(int(wait_node_index), len(edge_seconds)))
                hide_arrival_at = add_seconds(start_at, sum(edge_seconds[:edge_count]))
            except (TypeError, ValueError):
                hide_arrival_at = None
        hide_depart_at = add_seconds(hide_arrival_at, wait_seconds) if hide_arrival_at else None
        travel_seconds = sum(edge_seconds)
        launch_arrival_at = add_seconds(start_at, travel_seconds + wait_seconds) if fire_plan else None
        startup_sec = float(
            fire_plan.get("launch_startup_sec", 0.0)
            or fire_plan.get("path_report", {}).get("launch_startup_sec", 0.0)
            or 0.0
        )
        startup_mode = str(
            fire_plan.get("launch_startup_mode")
            or fire_plan.get("path_report", {}).get("launch_startup_mode")
            or "-"
        )
        fire_ready_at = add_seconds(launch_arrival_at, startup_sec) if launch_arrival_at else None
        fired_evt = next((r for r in vehicle_events_by_sid.get(sid, []) if r.get("event_name") == "FIRED"), {})
        redundant_ready_evt = next((r for r in vehicle_events_by_sid.get(sid, []) if r.get("event_name") == "REDUNDANT_READY"), {})
        fired_at = fire_ready_at or fired_evt.get("ts") or fire_plan.get("end_at")
        is_redundant = bool(assigned.get("redundant") or fire_plan.get("redundant"))
        if is_redundant:
            redundant_idx += 1
        else:
            mission_idx += 1

        parts: List[str] = []
        if assigned:
            score = assigned.get("score_breakdown") or {}
            score_text = ""
            if isinstance(score, dict) and score.get("total_score") is not None:
                project_book = score.get("project_book") or {}
                if isinstance(project_book, dict) and project_book.get("enabled"):
                    depot_book = project_book.get("depot_book") or {}
                    depot_text = ""
                    if not prelaunch_only and isinstance(depot_book, dict) and depot_book.get("enabled"):
                        depot_text = (
                            f"，贮备库附加评分 {depot_book.get('total_score')}({depot_book.get('depot_id')})"
                        )
                    score_text = (
                        f"，综合评分 {score.get('total_score')}，项目书附加代价 {project_book.get('composite_cost')}{depot_text}"
                    )
                else:
                    score_text = f"，综合评分 {score.get('total_score')}"
            redundant_text = "，冗余备份车辆" if is_redundant else ""
            parts.append(
                f"{fmt_hm(assigned.get('ts'))} 分配给 {vehicle_id}{redundant_text}，目标发射点 {launch_node}，计划发射 {fmt_hm(fire_time)}{score_text}"
            )
        if fire_plan:
            parts.append(f"{fmt_hm(start_at)} 开始执行发射前机动")
            if wait_node_id and hide_arrival_at:
                parts.append(
                    f"{fmt_hm(hide_arrival_at)} 到达隐蔽点 {wait_node_id}，待机 {human_duration(wait_seconds)}，{fmt_hm(hide_depart_at)} 离开隐蔽点"
                )
            elif wait_seconds > 0:
                parts.append(f"在当前位置待机 {human_duration(wait_seconds)}，用于对齐发射窗口")
            parts.append(f"{fmt_hm(launch_arrival_at)} 到达发射点 {launch_node}")
            if startup_sec > 0:
                parts.append(f"到点后执行 {startup_mode} 启动准备 {human_duration(startup_sec)}")
            if is_redundant:
                parts.append(f"{fmt_hm(fired_at or redundant_ready_evt.get('ts'))} 冗余车辆到位，不执行发射")
            else:
                parts.append(f"{fmt_hm(fired_at)} 完成发射")
        if not prelaunch_only and to_depot:
            parts.append(
                f"{fmt_hm(to_depot.get('start_at'))} 前往贮备库 {to_depot.get('target_node')}，{fmt_hm(to_depot.get('end_at'))} 到达"
            )
        if not prelaunch_only and reload_plan:
            parts.append(
                f"{fmt_hm(reload_plan.get('start_at'))} 开始装弹，{fmt_hm(reload_plan.get('end_at'))} 装弹完成"
            )
        depot_assignment = depot_assignments_by_sid.get(sid, {})
        if not prelaunch_only and depot_assignment:
            parts.append(
                f"独立贮备库模块推荐 {depot_assignment.get('selected_depot')}，候选库 {depot_assignment.get('depot_candidates')} 个"
            )
        if not prelaunch_only and return_plan:
            parts.append(
                f"{fmt_hm(return_plan.get('start_at'))} 返航，{fmt_hm(return_plan.get('end_at'))} 回到待命点"
            )

        task_label = sid.rsplit("_s", 1)[0] if "_s" in sid else sid
        if is_redundant:
            redundant_for = str(assigned.get("redundant_for_subtask_id") or fire_plan.get("redundant_for_subtask_id") or "")
            paired_text = f"，对应 {redundant_for}" if redundant_for else ""
            prefix = f"冗余{redundant_idx}"
            lines.append(f"- {prefix}（{task_label} / {sid}{paired_text}）：{'；'.join(parts) if parts else '暂无完整执行链路'}。")
        else:
            lines.append(f"- 任务{mission_idx}（{task_label} / {sid}）：{'；'.join(parts) if parts else '暂无完整执行链路'}。")

    return lines


def build_event_lines(
    scheduler_rows: List[Dict[str, Any]],
    vehicle_rows: List[Dict[str, Any]],
    final_state: Dict[str, Any],
    *,
    prelaunch_only: bool = False,
) -> List[str]:
    items: List[Tuple[datetime, int, str]] = []
    launch_by_subtask: Dict[str, str] = {}
    task_by_subtask: Dict[str, str] = {}
    vehicle_by_subtask: Dict[str, str] = {}
    target_by_subtask_phase: Dict[Tuple[str, str], str] = {}

    priority = {
        "task_received": 10,
        "subtask_assigned": 20,
        "subtask_reserved_after_reload": 22,
        "path_rejected": 30,
        "path_request_retry": 31,
        "reservation_deadlock_risk": 32,
        "plan_execute": 40,
        "vehicle_event": 50,
        "request_depot": 60,
        "depot_queue_join": 62,
        "depot_reload_start": 64,
        "depot_reload_complete": 66,
        "direct_dispatch_after_reload": 68,
        "request_return": 70,
        "subtask_done": 80,
        "subtask_reset": 90,
        "summary": 999,
    }

    plan_start_by_phase: Dict[Tuple[str, str], str] = {}
    plan_end_by_phase: Dict[Tuple[str, str], str] = {}
    for row in scheduler_rows:
        if row.get("event") != "plan_execute":
            continue
        sid = str(row.get("subtask_id") or "")
        phase = str(row.get("phase") or "")
        if sid and phase:
            if row.get("start_at"):
                plan_start_by_phase[(sid, phase)] = str(row.get("start_at"))
            if row.get("end_at"):
                plan_end_by_phase[(sid, phase)] = str(row.get("end_at"))

    for row in scheduler_rows:
        evt = row.get("event")
        sid = str(row.get("subtask_id") or "")
        display_ts = str(row.get("ts") or "")
        if evt == "plan_execute":
            display_ts = str(row.get("start_at") or display_ts)
        elif not prelaunch_only and evt == "request_depot":
            display_ts = plan_end_by_phase.get((sid, "to_fire"), display_ts)
        elif not prelaunch_only and evt == "depot_queue_join":
            display_ts = plan_end_by_phase.get((sid, "to_depot"), display_ts)
        elif not prelaunch_only and evt == "depot_reload_start":
            display_ts = plan_start_by_phase.get((sid, "reload_at_depot"), display_ts)
        elif not prelaunch_only and evt == "depot_reload_complete":
            display_ts = plan_end_by_phase.get((sid, "reload_at_depot"), display_ts)
        elif not prelaunch_only and evt == "request_return":
            display_ts = plan_end_by_phase.get((sid, "reload_at_depot"), plan_end_by_phase.get((sid, "to_fire"), display_ts))
        elif evt == "subtask_done":
            display_ts = (
                plan_end_by_phase.get((sid, "to_fire"), display_ts)
                if prelaunch_only
                else plan_end_by_phase.get((sid, "return_home"), display_ts)
            )
        ts = parse_ts(display_ts)
        if not ts:
            continue
        if evt == "task_received":
            redundant_count = int(row.get("redundant_launches", 0) or 0)
            redundant_text = f"，另配置 {redundant_count} 个冗余车辆任务" if redundant_count > 0 else ""
            text = (
                f"{fmt_hm(display_ts)} | 任务进入 | {row.get('task_id')} 进入系统，"
                f"本波包含 {row.get('launches')} 个发射子任务{redundant_text}。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "subtask_assigned":
            launch_by_subtask[sid] = str(row.get("launch_point") or "-")
            vehicle_by_subtask[sid] = str(row.get("vehicle_id") or "-")
            task_id = sid.rsplit("_s", 1)[0] if "_s" in sid else ""
            task_by_subtask[sid] = task_id
            score = row.get("score_breakdown") or {}
            score_text = ""
            if isinstance(score, dict) and score:
                fire_zone_note = "，位于火力区" if score.get("in_fire_zone") else ""
                project_book = score.get("project_book") or {}
                project_book_text = ""
                if isinstance(project_book, dict) and project_book.get("enabled"):
                    depot_book = project_book.get("depot_book") or {}
                    depot_book_text = ""
                    if not prelaunch_only and isinstance(depot_book, dict) and depot_book.get("enabled"):
                        depot_book_text = (
                            f"，贮备库评分 {depot_book.get('total_score')} "
                            f"(目标 {depot_book.get('depot_id')}，繁忙 {depot_book.get('busy_score')}，"
                            f"等待 {depot_book.get('prep_score')}，时间 {depot_book.get('time_score')})"
                        )
                    project_book_text = (
                        f"，项目书附加代价 {project_book.get('composite_cost')} "
                        f"(机动 {project_book.get('distance_cost_km')}km，隐蔽 {project_book.get('nearest_hide_distance_km')}km，"
                        f"支撑 {project_book.get('support_suitability_score')})"
                        f"{depot_book_text}"
                    )
                score_text = (
                    f"，综合评分 {score.get('total_score')} "
                    f"(距离 {score.get('distance_score')}，时间 {score.get('travel_time_score')}，"
                    f"火力区 {score.get('fire_zone_score')}，迟到扣分 {score.get('lateness_penalty')}{fire_zone_note})"
                    f"{project_book_text}"
                )
            text = (
                f"{fmt_hm(display_ts)} | 点位分配 | {sid} 分配给 {row.get('vehicle_id')}，"
                f"目标发射点 {row.get('launch_point')}，计划发射时间 {fmt_hm(row.get('fire_time'))}"
                f"{'，标记为冗余车辆' if row.get('redundant') else ''}{score_text}。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "subtask_reserved_after_reload":
            score = row.get("score_breakdown") or {}
            score_text = ""
            if isinstance(score, dict) and score:
                project_book = score.get("project_book") or {}
                if isinstance(project_book, dict) and project_book.get("enabled"):
                    depot_book = project_book.get("depot_book") or {}
                    depot_text = ""
                    if not prelaunch_only and isinstance(depot_book, dict) and depot_book.get("enabled"):
                        depot_text = (
                            f"，贮备库附加评分 {depot_book.get('total_score')}({depot_book.get('depot_id')})"
                        )
                    score_text = (
                        f"，综合评分 {score.get('total_score')}，项目书附加代价 {project_book.get('composite_cost')}{depot_text}"
                    )
                else:
                    score_text = f"，综合评分 {score.get('total_score')}"
            text = (
                f"{fmt_hm(display_ts)} | 滚动接单 | {sid} 预留给 {row.get('vehicle_id')}，"
                f"该车当前处于 {row.get('assignment_mode')}，将在装弹完成后直接前往发射点 {row.get('launch_point')}{score_text}。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "plan_execute":
            phase = str(row.get("phase") or "-")
            target_by_subtask_phase[(sid, phase)] = str(row.get("target_node") or "-")
            start_at = row.get("start_at")
            end_at = row.get("end_at")
            duration = human_duration(seconds_between(start_at, end_at))
            delay = float(row.get("delay_sec", 0.0) or 0.0)
            extra = ""
            if delay > 0:
                extra = f"，因轨迹冲突延后 {delay:.0f} 秒放行"
            if phase == "to_fire":
                label = f"开始向发射点 {row.get('target_node')} 行驶"
                hide_point = row.get("wait_node_id") or row.get("path_report", {}).get("hide_point")
                startup_mode = row.get("launch_startup_mode") or row.get("path_report", {}).get("launch_startup_mode")
                startup_sec = float(row.get("launch_startup_sec", 0.0) or row.get("path_report", {}).get("launch_startup_sec", 0.0) or 0.0)
                if hide_point:
                    extra += f"，将先经隐蔽点 {hide_point} 待机"
                elif float(row.get("wait_seconds", 0.0) or 0.0) > 0:
                    extra += f"，先在当前位置待机 {human_duration(float(row.get('wait_seconds', 0.0) or 0.0))} 以对齐发射时间"
                if startup_mode:
                    extra += f"，到点后执行 {startup_mode} 启动 {human_duration(startup_sec)}"
                if row.get("suppress_fire"):
                    extra += "，该车为冗余备份，到点后不发射"
            elif phase == "to_depot":
                label = f"开始向贮备库 {row.get('target_node')} 行驶"
            elif phase == "reload_at_depot":
                label = f"开始在贮备库 {row.get('target_node')} 装弹"
            elif phase == "return_home":
                label = "开始返航回待命点"
            else:
                label = f"开始执行阶段 {phase}"
            text = (
                f"{fmt_hm(display_ts)} | 轨迹下发 | {row.get('vehicle_id')} 执行 {sid} 的 {phase} 阶段，"
                f"{label}，预计耗时 {duration}，轨迹点 {len(row.get('trajectory') or [])}{extra}。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "path_rejected":
            text = (
                f"{fmt_hm(display_ts)} | 轨迹重规划 | {row.get('vehicle_id')} 的 {sid} 路径被拒绝，"
                f"原因 {row.get('reason')}，系统准备重新求解。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "path_request_retry":
            text = (
                f"{fmt_hm(display_ts)} | 轨迹重规划 | {sid} 向 {row.get('vehicle_id')} 发起第 {row.get('retry')} 次路径重试。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "reservation_deadlock_risk":
            text = (
                f"{fmt_hm(display_ts)} | 轨迹冲突 | {sid} 在 {row.get('phase')} 阶段出现持续占道冲突，"
                f"系统触发退避重排。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "request_depot":
            text = (
                f"{fmt_hm(display_ts)} | 阶段切换 | {sid} 完成发射后，"
                f"{row.get('vehicle_id')} 转向贮备库 {row.get('depot')}。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "depot_queue_join":
            text = (
                f"{fmt_hm(display_ts)} | 贮备库排队 | {row.get('vehicle_id')} 到达贮备库 {row.get('depot')}，"
                f"当前前方排队序号 {row.get('queue_position')}。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "depot_reload_start":
            text = (
                f"{fmt_hm(display_ts)} | 贮备库装弹 | {row.get('vehicle_id')} 进入贮备库 {row.get('depot')} 装弹，"
                f"预计装弹时长 {human_duration(float(row.get('reload_seconds', 0.0) or 0.0))}。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "depot_reload_complete":
            text = (
                f"{fmt_hm(display_ts)} | 贮备库装弹 | {row.get('vehicle_id')} 在贮备库 {row.get('depot')} 完成装弹。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "direct_dispatch_after_reload":
            text = (
                f"{fmt_hm(display_ts)} | 滚动接单 | {row.get('vehicle_id')} 完成 {row.get('completed_subtask_id')} 装弹后，"
                f"不返航，直接转入 {row.get('next_subtask_id')} 的执行流程。"
            )
            items.append((ts, priority[evt], text))
        elif not prelaunch_only and evt == "request_return":
            return_reason = "冗余到位后" if sid and any(
                r.get("event") == "plan_execute" and r.get("subtask_id") == sid and r.get("suppress_fire")
                for r in scheduler_rows
            ) else "完成补给后"
            text = (
                f"{fmt_hm(display_ts)} | 阶段切换 | {sid} {return_reason}，"
                f"{row.get('vehicle_id')} 开始返航。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "subtask_done":
            done_text = ("已完成冗余到位闭环" if prelaunch_only else "已完成冗余到位和返航闭环") if any(
                r.get("event") == "plan_execute" and r.get("subtask_id") == sid and r.get("suppress_fire")
                for r in scheduler_rows
            ) else ("已完成发射闭环" if prelaunch_only else "已完成发射、补给和返航闭环")
            text = (
                f"{fmt_hm(display_ts)} | 子任务完成 | {sid} {done_text}。"
            )
            items.append((ts, priority[evt], text))
        elif evt == "subtask_reset":
            text = (
                f"{fmt_hm(display_ts)} | 异常回收 | {sid} 被系统回收，"
                f"原因 {row.get('reason')}，状态 {row.get('status')}。"
            )
            items.append((ts, priority[evt], text))

    simulated_vehicle_ts: Dict[Tuple[str, str], str] = {}
    phase_event = {
        "to_fire": "FIRED",
        "to_depot": "ARRIVED_DEPOT",
        "reload_at_depot": "RELOADED",
        "return_home": "IDLE_READY",
    }
    for row in scheduler_rows:
        if row.get("event") != "plan_execute":
            continue
        sid = str(row.get("subtask_id") or "")
        event_name = phase_event.get(str(row.get("phase") or ""))
        if row.get("suppress_fire") and row.get("phase") == "to_fire":
            event_name = "REDUNDANT_READY"
        end_at = row.get("end_at")
        if sid and event_name and end_at:
            simulated_vehicle_ts[(sid, event_name)] = str(end_at)

    for row in vehicle_rows:
        evt = row.get("event")
        if evt != "vehicle_event":
            continue
        sid = str(row.get("subtask_id") or "")
        name = row.get("event_name")
        display_ts = simulated_vehicle_ts.get((sid, str(name)), row.get("ts"))
        ts = parse_ts(display_ts)
        if not ts:
            continue
        if name == "FIRED":
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 在 {row.get('current_node')} 完成 {sid} 发射动作。"
            )
        elif name == "REDUNDANT_READY":
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 作为冗余车辆到达 {row.get('current_node')}，不执行发射。"
            )
        elif not prelaunch_only and name == "ARRIVED_DEPOT":
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 到达贮备库 {row.get('current_node')}，等待调度安排装弹。"
            )
        elif not prelaunch_only and name == "RELOADED":
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 完成 {sid} 补给。"
            )
        elif not prelaunch_only and name == "IDLE_READY":
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 完成 {sid} 返航并恢复待命。"
            )
        else:
            text = (
                f"{fmt_hm(display_ts)} | 车辆动作 | {row.get('node_id')} 上报 {sid} 事件 {name}。"
            )
        items.append((ts, priority["vehicle_event"], text))

    candidate_last_ts = [parse_ts(r.get("ts")) for r in scheduler_rows if r.get("ts")]
    candidate_last_ts.extend(parse_ts(r.get("end_at")) for r in scheduler_rows if r.get("event") == "plan_execute" and r.get("end_at"))
    last_ts = max((ts for ts in candidate_last_ts if ts), default=None)
    if last_ts:
        subtasks = final_state.get("subtasks", [])
        by_task: Dict[str, Counter] = defaultdict(Counter)
        for sub in subtasks:
            kind = "冗余任务" if bool(sub.get("redundant")) else "真实任务"
            by_task[str(sub.get("task_id") or "-")][f"{kind}{str(sub.get('status') or '-')}"] += 1

        mission = [s for s in subtasks if not bool(s.get("redundant"))]
        redundant = [s for s in subtasks if bool(s.get("redundant"))]
        mission_pending = sum(1 for s in mission if s.get("status") == "PENDING")
        mission_done = sum(1 for s in mission if s.get("status") == "DONE")
        mission_running = len(mission) - mission_pending - mission_done
        redundant_pending = sum(1 for s in redundant if s.get("status") == "PENDING")
        redundant_done = sum(1 for s in redundant if s.get("status") == "DONE")
        redundant_running = len(redundant) - redundant_pending - redundant_done
        items.append(
            (
                last_ts,
                priority["summary"],
                f"{last_ts.astimezone(LOCAL_TZ).strftime('%H:%M:%S')} | 截止采集 | 多波次任务共注入 {len(mission)} 个真实发射任务，另配置 {len(redundant)} 个冗余车辆任务；真实任务已闭环 {mission_done} 个、执行中 {mission_running} 个、待调度 {mission_pending} 个；冗余任务已闭环 {redundant_done} 个、执行中 {redundant_running} 个、待调度 {redundant_pending} 个。",
            )
        )
        for task_id in sorted(by_task):
            status_counter = by_task[task_id]
            parts = []
            for raw_key, count in sorted(status_counter.items()):
                if raw_key.startswith("真实任务"):
                    parts.append(f"真实任务 {raw_key.removeprefix('真实任务')}:{count}")
                elif raw_key.startswith("冗余任务"):
                    parts.append(f"冗余任务 {raw_key.removeprefix('冗余任务')}:{count}")
                else:
                    parts.append(f"{raw_key}:{count}")
            items.append(
                (
                    last_ts,
                    priority["summary"] + 1,
                    f"{last_ts.astimezone(LOCAL_TZ).strftime('%H:%M:%S')} | 截止采集 | {task_id} 当前状态分布：{'，'.join(parts)}。",
                )
            )

        deadlock = sum(1 for r in scheduler_rows if r.get("event") == "reservation_deadlock_risk")
        rejected = sum(1 for r in scheduler_rows if r.get("event") == "path_rejected")
        retry = sum(1 for r in scheduler_rows if r.get("event") == "path_request_retry")
        delayed = [
            r for r in scheduler_rows
            if r.get("event") == "plan_execute" and float(r.get("delay_sec", 0.0) or 0.0) > 0
        ]
        if delayed or deadlock or rejected or retry:
            worst = max((float(r.get("delay_sec", 0.0) or 0.0) for r in delayed), default=0.0)
            items.append(
                (
                    last_ts,
                    priority["summary"] + 2,
                    f"{last_ts.astimezone(LOCAL_TZ).strftime('%H:%M:%S')} | 截止采集 | 本轮轨迹冲突实际延时 {len(delayed)} 次，最大延时 {worst:.0f} 秒；死锁退避 {deadlock} 次，路径拒绝 {rejected} 次，路径重试 {retry} 次。",
                )
            )
        else:
            items.append(
                (
                    last_ts,
                    priority["summary"] + 2,
                    f"{last_ts.astimezone(LOCAL_TZ).strftime('%H:%M:%S')} | 截止采集 | 未出现轨迹冲突延时、死锁退避、路径拒绝或路径重试。",
                )
            )

    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return [text for _, _, text in items]


def build_brief_text(
    run_root: Path,
    logs_dir: Path,
    out_dir: Path,
) -> str:
    scheduler_cfg = load_json(run_root / "configs" / "scheduler_debug.json")
    build_meta = load_json(run_root / "build_meta.json")
    final_state = load_json(run_root / "visuals" / "final_state.json")
    wave_dispatch_metrics = load_json(run_root / "results" / "wave_dispatch_metrics.json")
    scheduler_rows = filter_rows_for_final_state(load_jsonl(logs_dir / "scheduler_events.jsonl"), final_state)
    vehicle_rows: List[Dict[str, Any]] = []
    for path in sorted(logs_dir.glob("vehicle_*_events.jsonl")):
        vehicle_rows.extend(load_jsonl(path))
    vehicle_rows = filter_rows_for_final_state(vehicle_rows, final_state)
    depot_rows = filter_rows_for_final_state(load_jsonl(logs_dir / "depot_events.jsonl"), final_state)

    wave_lines = load_wave_timeline(run_root / "visuals" / "mission_timeline.txt")
    if not wave_lines:
        wave_lines = load_wave_timeline(run_root / "visuals" / "wave_timeline.txt")
    completion_lines = [parse_wave_line(line) for line in wave_lines if line.startswith("completion_wait=")]
    runtime_lines = [parse_wave_line(line) for line in wave_lines if line.startswith("runtime_check")]
    runtime_stage_lines = [parse_wave_line(line) for line in wave_lines if line.startswith("runtime_stage")]
    dispatch_by_task_id = {
        str(row.get("task_id") or ""): row
        for row in (wave_dispatch_metrics.get("waves", []) or [])
        if row.get("task_id")
    }
    wave_lines = [line for line in wave_lines if line.startswith("wave=")]
    valid_task_ids = {
        str(row.get("task_id") or "")
        for row in final_state.get("subtasks", []) or []
        if row.get("task_id")
    }
    if valid_task_ids:
        wave_lines = [
            line
            for line in wave_lines
            if str(parse_wave_line(line).get("task_id") or "") in valid_task_ids
        ]
    task_received = [r for r in scheduler_rows if r.get("event") == "task_received"]
    assigned = [r for r in scheduler_rows if r.get("event") == "subtask_assigned"]
    executed = [r for r in scheduler_rows if r.get("event") == "plan_execute"]
    fired = [r for r in vehicle_rows if r.get("event") == "vehicle_event" and r.get("event_name") == "FIRED"]
    depot_queue = [r for r in scheduler_rows if r.get("event") == "depot_queue_join"]
    reload_started = [r for r in scheduler_rows if r.get("event") == "depot_reload_start"]
    deferred = [r for r in scheduler_rows if r.get("event") == "subtask_reserved_after_reload"]
    direct_after_reload = [r for r in scheduler_rows if r.get("event") == "direct_dispatch_after_reload"]
    deadlock = [r for r in scheduler_rows if r.get("event") == "reservation_deadlock_risk"]
    rejected = [r for r in scheduler_rows if r.get("event") == "path_rejected"]
    retry = [r for r in scheduler_rows if r.get("event") == "path_request_retry"]
    depot_context_updates = [r for r in depot_rows if r.get("event") == "depot_assignment_context_received"]
    depot_score_queries = [r for r in depot_rows if r.get("event") == "depot_score_computed"]
    depot_assignment_results = [r for r in depot_rows if r.get("event") == "depot_assignment_computed"]
    selected_depot_counter = Counter(
        str(r.get("selected_depot") or "-")
        for r in depot_assignment_results
        if r.get("selected_depot")
    )
    fire_exec = [r for r in executed if r.get("phase") == "to_fire"]
    hide_used = [
        r for r in fire_exec
        if r.get("wait_node_id") or (isinstance(r.get("path_report"), dict) and r.get("path_report", {}).get("hide_selected"))
    ]
    startup_counter = Counter(
        str(r.get("launch_startup_mode") or (r.get("path_report", {}) if isinstance(r.get("path_report"), dict) else {}).get("launch_startup_mode") or "unknown")
        for r in fire_exec
    )
    delayed = [
        r for r in executed
        if float(r.get("delay_sec", 0.0) or 0.0) > 0
    ]

    metrics = final_state.get("metrics", {}) or {}
    final_subtasks = final_state.get("subtasks", [])
    total_subtasks = len(final_subtasks)
    mission_subtasks = [s for s in final_subtasks if not bool(s.get("redundant"))]
    redundant_subtasks = [s for s in final_subtasks if bool(s.get("redundant"))]
    mission_total = int(metrics.get("mission_tasks_total", len(mission_subtasks)))
    redundant_total = int(metrics.get("redundant_tasks_total", len(redundant_subtasks)))
    pending_subtasks = int(metrics.get("subtasks_pending", 0))
    running_subtasks = int(metrics.get("subtasks_running", 0))
    mission_pending = int(metrics.get("mission_tasks_pending", sum(1 for s in mission_subtasks if s.get("status") == "PENDING")))
    mission_running = int(metrics.get("mission_tasks_running", sum(1 for s in mission_subtasks if str(s.get("status")) not in {"PENDING", "DONE", "FAILED"})))
    redundant_pending = int(metrics.get("redundant_tasks_pending", sum(1 for s in redundant_subtasks if s.get("status") == "PENDING")))
    redundant_running = int(metrics.get("redundant_tasks_running", sum(1 for s in redundant_subtasks if str(s.get("status")) not in {"PENDING", "DONE", "FAILED"})))
    vehicles_online = int(metrics.get("vehicles_online", 0))

    lines: List[str] = []
    lines.append("运行简报")
    lines.append(f"生成时间：{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"结果目录：{out_dir}")
    lines.append("")
    lines.append("一、任务总结")
    prelaunch_only = bool(scheduler_cfg.get("prelaunch_only", False))
    lines.extend(build_task_summary_lines(scheduler_rows, vehicle_rows, depot_rows, prelaunch_only=prelaunch_only))
    lines.append("")
    lines.append("二、领导摘要")
    lines.append(
        f"- 本次演示基于北京城区路网，范围 {build_meta.get('bbox_lonlat')}，当前内部图规模为 {build_meta.get('graph_nodes', '-')} 个节点、{build_meta.get('graph_edges', '-')} 条边。"
    )
    fire_zone = scheduler_cfg.get("fire_zone") or {}
    if fire_zone.get("enabled"):
        lines.append(
            f"- 地图已设置火力区，当前规则为 {fire_zone.get('description', 'half_map')}，位于火力区内的车辆在任务分配时可额外获得 {fire_zone.get('bonus_score', 0)} 分。"
        )
    if fire_exec:
        startup_parts = [f"{k}:{v}" for k, v in sorted(startup_counter.items())]
        lines.append(
            f"- 发射前准备已纳入规划：本轮去发射点计划中，使用隐蔽点待机 {len(hide_used)} 次；启动模式分布为 {'，'.join(startup_parts)}。"
        )
    if completion_lines:
        status = str(completion_lines[-1].get("completion_wait", "")).strip()
        if status.startswith("all_done"):
            lines.append("- 本轮结果在全部子任务清空后导出，当前结果文件可视为完整终态。")
        elif status.startswith("timeout"):
            lines.append(f"- 本轮结果在等待全量收敛时达到超时上限后导出，当前结果为超时截取快照：{status}。")
    if runtime_lines:
        runtime = runtime_lines[-1]
        planning_ready_wall_sec = float(runtime.get("planning_ready_wall_sec", runtime.get("scheduling_wall_sec", 0.0)) or 0.0)
        closed_loop_wall_sec = float(runtime.get("closed_loop_wall_sec", planning_ready_wall_sec) or 0.0)
        full_wall_sec = float(runtime.get("full_wall_sec", 0.0) or 0.0)
        target_sec = float(runtime.get("target_sec", 10.0) or 10.0)
        pass_10s = str(runtime.get("pass_10s", "")).lower() == "true"
        verdict = "满足" if pass_10s else "未满足"
        lines.append(
            f"- 运行时间检测：从第一波任务下发到真实任务规划结果全部就绪，真实耗时 {planning_ready_wall_sec:.3f} 秒，"
            f"甲方 {target_sec:.0f} 秒阈值评估为{verdict}；该口径不包含截图渲染和领导简报文本生成。"
            + (
                f"按当前射前闭环口径，阶段闭环耗时 {closed_loop_wall_sec:.3f} 秒；"
                if prelaunch_only
                else f"若按补给与返航全部闭环计算，耗时 {closed_loop_wall_sec:.3f} 秒；"
            )
            + f"脚本完整耗时 {full_wall_sec:.3f} 秒，包含建图、可选渲染、启动进程和状态采集等演示开销。"
        )
        if runtime_stage_lines:
            stage_items = []
            for row in runtime_stage_lines:
                label = row.get("label") or row.get("key") or "-"
                try:
                    wall_sec = float(row.get("wall_sec", 0.0) or 0.0)
                except (TypeError, ValueError):
                    wall_sec = 0.0
                stage_items.append((wall_sec, label))
            stage_items.sort(reverse=True)
            top_parts = [f"{label} {wall_sec:.3f}s" for wall_sec, label in stage_items[:8]]
            lines.append(f"- 耗时拆解：{'；'.join(top_parts)}。")
        else:
            lines.append("- 耗时拆解：当前结果只记录了总耗时，未记录分阶段耗时；下次运行新版脚本后会自动列出建图、可选渲染、启动、任务注入、等待闭环、状态采集和导出的分项耗时。")
    else:
        lines.append("- 运行时间检测：当前结果未找到 runtime_metrics.jsonl 计时记录，无法按 10 秒阈值评估；请确认启动流程包含运行时间采集。")
    lines.append(
        f"- 本轮共注入 {len(task_received)} 波任务，累计 {mission_total} 个真实发射任务，另配置 {redundant_total} 个冗余车辆任务；"
        f"截至采集结束，真实任务执行中 {mission_running} 个、等待 {mission_pending} 个，冗余任务执行中 {redundant_running} 个、等待 {redundant_pending} 个。"
    )
    if dispatch_by_task_id:
        lines.append("- 各波次调度下发耗时如下：")
        for row in sorted(
            dispatch_by_task_id.values(),
            key=lambda item: int(item.get("wave_index", 0) or 0),
        ):
            mission_sec = row.get("mission_dispatch_wall_sec")
            all_sec = row.get("all_dispatch_wall_sec")
            mission_done = row.get("mission_to_fire_plan_count")
            mission_expected = row.get("mission_expected")
            all_done = row.get("all_to_fire_plan_count")
            all_expected = row.get("all_expected")
            lines.append(
                f"  波次{row.get('wave_index')}: 真任务下发 {mission_done}/{mission_expected}，耗时 {mission_sec}s，"
                f"完成时间 {row.get('mission_dispatch_complete_at_local') or '-'}；"
                f"含冗余下发 {all_done}/{all_expected}，耗时 {all_sec}s，完成时间 {row.get('all_dispatch_complete_at_local') or '-'}。"
            )
    lines.append(
        f"- 当前共有 {len(scheduler_cfg.get('vehicles', []))} 辆车参与，实际在线 {vehicles_online} 辆；已完成点位分配 {len(assigned)} 次，已正式下发轨迹 {len(executed)} 条。"
    )
    lines.append(
        f"- 多波次任务采用滚动调度：后续波次到达后先进入待调度队列，不打断已下发车辆；若前序车辆占道，则通过轨迹预约延时错峰放行。"
    )
    if not prelaunch_only:
        lines.append(
            f"- 贮备库按单库位装弹，当前观察到排队 {len(depot_queue)} 次、启动装弹 {len(reload_started)} 次；如空闲车辆不足，系统可预留排队/装弹中的车辆，在装弹完成后直接转入新任务。"
        )
    if not prelaunch_only and (depot_context_updates or depot_score_queries or depot_assignment_results):
        depot_top = "，".join(
            f"{depot_id}:{count}"
            for depot_id, count in selected_depot_counter.most_common(3)
        ) or "无明确选中库"
        lines.append(
            f"- 独立贮备库软件已接收分配上下文 {len(depot_context_updates)} 次，完成库评分 {len(depot_score_queries)} 次、库分配 {len(depot_assignment_results)} 次；当前推荐最多的贮备库为 {depot_top}。"
        )
    if delayed or deadlock or rejected or retry:
        max_delay = max((float(r.get("delay_sec", 0.0) or 0.0) for r in delayed), default=0.0)
        lines.append(
            f"- 运行中轨迹冲突实际延时 {len(delayed)} 次，最大延时 {max_delay:.0f} 秒；死锁退避 {len(deadlock)} 次，路径拒绝 {len(rejected)} 次，路径重试 {len(retry)} 次。"
        )
    else:
        lines.append("- 运行中未出现死锁、路径拒绝或路径重试，整体执行平稳。")
    done_subtasks = sum(1 for s in final_subtasks if str(s.get("status")) == "DONE")
    done_mission = sum(1 for s in mission_subtasks if str(s.get("status")) == "DONE")
    done_redundant = sum(1 for s in redundant_subtasks if str(s.get("status")) == "DONE")
    unfinished_mission = mission_total - done_mission
    unfinished_redundant = redundant_total - done_redundant
    lines.append(
        f"- 截至采集结束，车辆已完成发射 {len(fired)} 次；真实任务已完整{'射前' if prelaunch_only else ''}闭环 {done_mission} 个、未闭环 {unfinished_mission} 个；"
        f"冗余车辆任务已完整{'冗余到位' if prelaunch_only else ''}闭环 {done_redundant} 个、未闭环 {unfinished_redundant} 个。"
    )
    if not prelaunch_only and (deferred or direct_after_reload):
        lines.append(
            f"- 本轮已触发装弹后直接接续新任务 {len(direct_after_reload)} 次，预留给装弹/排队车辆的后续任务 {len(deferred)} 次。"
        )
    lines.append("")
    lines.append("三、时间线条目")
    if wave_lines:
        for wave_idx, raw in enumerate(wave_lines, start=1):
            meta = parse_wave_line(raw)
            sent_at = meta.get("sent_at", "-")
            fire_after = meta.get("fire_after")
            interval = meta.get("interval")
            timing_note = ""
            if fire_after or interval:
                timing_parts = []
                if fire_after:
                    timing_parts.append(f"首发时间相对发送时刻 {fire_after}")
                if interval:
                    timing_parts.append(f"子任务时间间隔 {interval}")
                timing_note = "，" + "，".join(timing_parts)
            dispatch_metric = dispatch_by_task_id.get(str(meta.get("task_id") or ""))
            dispatch_note = ""
            if dispatch_metric:
                dispatch_note = (
                    f"，真任务调度下发耗时 {dispatch_metric.get('mission_dispatch_wall_sec')}s"
                    f"，含冗余下发耗时 {dispatch_metric.get('all_dispatch_wall_sec')}s"
                )
            lines.append(
                f"- {sent_at} | 波次计划 | 第 {wave_idx} 波计划发送，包含 {meta.get('launches', '?')} 个真实发射任务，"
                f"另含 {meta.get('redundant_launches', 0)} 个冗余车辆任务{timing_note}{dispatch_note}。"
            )

    for text in build_event_lines(scheduler_rows, vehicle_rows, final_state, prelaunch_only=prelaunch_only):
        lines.append(f"- {text}")

    lines.append("")
    lines.append("四、补充说明")
    lines.append("- 本文档只保留领导汇报所需的关键过程，不展开原始心跳、逐点轨迹坐标等底层数据。")
    lines.append(
        "- 地图总览图单独输出在同一目录，用于说明车辆初始待命位置、隐蔽点和发射点分布。"
        if prelaunch_only
        else "- 地图总览图单独输出在同一目录，用于说明车辆初始待命位置、隐蔽点、发射点和贮备库分布。"
    )
    lines.append("- 如需进一步查看每条轨迹的时间戳点集，可回看原始结果文件 planning_results.json。")
    lines.append("- 按数据文件说明同步导出 stage_timeline.json、point_assignment.json、realtime_pose.json，并在领导包中复制为 07/08/09 三个标准接口文件。")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a leader-friendly run brief and map overview.")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    run_root = Path(args.run_root).resolve()
    logs_dir = Path(args.logs_dir).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else run_root / "results" / "leader_package"
    out_dir.mkdir(parents=True, exist_ok=True)

    brief_text = build_brief_text(run_root, logs_dir, out_dir)
    brief_path = out_dir / "01_run_brief.txt"
    brief_path.write_text(brief_text, encoding="utf-8")

    scheduler_cfg = load_json(run_root / "configs" / "scheduler_debug.json")
    map_path = out_dir / "02_map_overview.png"
    render_map_overview(scheduler_cfg, map_path)

    source_plan = run_root / "results" / "planning_results.json"
    if source_plan.exists():
        shutil.copy2(source_plan, out_dir / "03_planning_results.json")
    for source_name, target_name in (
        ("stage_timeline.json", "07_stage_timeline.json"),
        ("point_assignment.json", "08_point_assignment.json"),
        ("realtime_pose.json", "09_realtime_pose.json"),
    ):
        source_file = run_root / "results" / source_name
        if source_file.exists():
            shutil.copy2(source_file, out_dir / target_name)

    final_state = load_json(run_root / "visuals" / "final_state.json")
    scheduler_rows = filter_rows_for_final_state(load_jsonl(logs_dir / "scheduler_events.jsonl"), final_state)
    vehicle_rows: List[Dict[str, Any]] = []
    for path in sorted(logs_dir.glob("vehicle_*_events.jsonl")):
        vehicle_rows.extend(load_jsonl(path))
    vehicle_rows = filter_rows_for_final_state(vehicle_rows, final_state)
    depot_rows = filter_rows_for_final_state(load_jsonl(logs_dir / "depot_events.jsonl"), final_state)
    detail_text = summarize_subtask_lifecycles(
        scheduler_rows,
        vehicle_rows,
        depot_rows,
        final_state,
        prelaunch_only=bool(scheduler_cfg.get("prelaunch_only", False)),
    )
    (out_dir / "04_subtask_details.txt").write_text(detail_text, encoding="utf-8")


if __name__ == "__main__":
    main()
