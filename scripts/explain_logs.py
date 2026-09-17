#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


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


def select_rows(
    rows: Iterable[Dict[str, Any]],
    task_id: Optional[str],
    subtask_id: Optional[str],
    vehicle_id: Optional[str],
) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if task_id:
            tid = row.get("task_id")
            sid = row.get("subtask_id", "")
            if tid != task_id and not str(sid).startswith(task_id):
                continue
        if subtask_id and row.get("subtask_id") != subtask_id:
            continue
        if vehicle_id and row.get("vehicle_id") != vehicle_id and row.get("node_id") != vehicle_id:
            continue
        out.append(row)
    return out


def explain_event(row: Dict[str, Any]) -> str:
    ts = row.get("ts", "?")
    event = row.get("event", "?")
    if event == "heartbeat":
        return (
            f"{ts} 调度收到心跳: 车辆 {row.get('vehicle_id')} 在线, "
            f"状态={row.get('status')}, 位置={row.get('current_node')}"
        )
    if event == "task_received":
        return f"{ts} 调度收到任务包 {row.get('task_id')}, 共 {row.get('launches')} 个发射子任务"
    if event == "subtask_assigned":
        return (
            f"{ts} 子任务 {row.get('subtask_id')} 被分配给 {row.get('vehicle_id')}, "
            f"目标发射点={row.get('launch_point')}, 要求发射时刻={row.get('fire_time')}"
        )
    if event == "path_proposal":
        extra = []
        if row.get("phase"):
            extra.append(f"阶段={row.get('phase')}")
        if row.get("path_nodes") is not None:
            extra.append(f"路径节点数={row.get('path_nodes')}")
        if row.get("wait_sec") is not None:
            extra.append(f"等待={row.get('wait_sec')}s")
        return f"{ts} 路径提案: 子任务 {row.get('subtask_id')} 车辆={row.get('vehicle_id', row.get('node_id'))} " + ", ".join(extra)
    if event == "plan_execute":
        return (
            f"{ts} 调度批准执行: 子任务 {row.get('subtask_id')} 车辆={row.get('vehicle_id')} "
            f"阶段={row.get('phase')} 冲突延迟={row.get('delay_sec')}s "
            f"开始={row.get('start_at')} 结束={row.get('end_at')}"
        )
    if event == "request_depot":
        return f"{ts} 发射后转入补给: 子任务 {row.get('subtask_id')} 车辆={row.get('vehicle_id')} 贮备库={row.get('depot')}"
    if event == "request_return":
        return f"{ts} 补给后转入返航: 子任务 {row.get('subtask_id')} 车辆={row.get('vehicle_id')} 回到={row.get('home')}"
    if event == "vehicle_event":
        return (
            f"{ts} 车辆事件: 车辆 {row.get('node_id')} 子任务 {row.get('subtask_id')} "
            f"事件={row.get('event_name')} 位置={row.get('current_node')} 剩余弹药={row.get('ammo_count')}"
        )
    if event == "subtask_done":
        return f"{ts} 子任务完成: {row.get('subtask_id')} 车辆={row.get('vehicle_id')}"
    if event == "subtask_reset":
        return (
            f"{ts} 子任务被回收/重置: {row.get('subtask_id')} 原因={row.get('reason')} "
            f"阶段={row.get('phase')} 新状态={row.get('status')}"
        )
    if event == "path_request_retry":
        return (
            f"{ts} 路径请求重试: 子任务 {row.get('subtask_id')} 车辆={row.get('vehicle_id')} "
            f"阶段={row.get('phase')} 第 {row.get('retry')} 次"
        )
    if event == "execute_plan":
        return (
            f"{ts} 车辆开始执行: 车辆 {row.get('node_id')} 子任务 {row.get('subtask_id')} "
            f"阶段={row.get('phase')} 路径节点数={row.get('path_nodes')}"
        )
    return f"{ts} 未分类事件: {json.dumps(row, ensure_ascii=False)}"


def summarize(rows: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    if not rows:
        return ["没有匹配到日志事件"]

    task_ids = sorted({r.get("task_id") for r in rows if r.get("task_id")})
    subtask_ids = sorted({r.get("subtask_id") for r in rows if r.get("subtask_id")})
    vehicle_ids = sorted(
        {
            r.get("vehicle_id") or r.get("node_id")
            for r in rows
            if r.get("vehicle_id") or r.get("node_id")
        }
    )
    lines.append(f"匹配事件数: {len(rows)}")
    if task_ids:
        lines.append(f"涉及任务: {', '.join(task_ids)}")
    if subtask_ids:
        lines.append(f"涉及子任务: {', '.join(subtask_ids[:12])}")
    if vehicle_ids:
        lines.append(f"涉及车辆: {', '.join(vehicle_ids[:12])}")
    lines.append("")
    lines.append("时间线:")
    for row in rows:
        lines.append(f"- {explain_event(row)}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate event logs into readable Chinese timeline")
    parser.add_argument("--task-id")
    parser.add_argument("--subtask-id")
    parser.add_argument("--vehicle-id")
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl")
    parser.add_argument("--vehicle-log-dir", default="logs")
    args = parser.parse_args()

    scheduler_rows = load_jsonl(Path(args.scheduler_log))
    vehicle_rows: List[Dict[str, Any]] = []
    for path in sorted(Path(args.vehicle_log_dir).glob("vehicle_*_events.jsonl")):
        vehicle_rows.extend(load_jsonl(path))

    all_rows = scheduler_rows + vehicle_rows
    selected = select_rows(
        rows=all_rows,
        task_id=args.task_id,
        subtask_id=args.subtask_id,
        vehicle_id=args.vehicle_id,
    )
    selected.sort(key=lambda x: x.get("ts", ""))
    print("\n".join(summarize(selected)))


if __name__ == "__main__":
    main()
