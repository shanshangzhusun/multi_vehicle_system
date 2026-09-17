#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize one task from scheduler logs")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--scheduler-log", default="logs/scheduler_events.jsonl")
    args = parser.parse_args()

    rows = load_jsonl(Path(args.scheduler_log))
    rows = [r for r in rows if r.get("task_id") == args.task_id or str(r.get("subtask_id", "")).startswith(args.task_id)]
    rows.sort(key=lambda x: x.get("ts", ""))

    if not rows:
        print("没有匹配到任务日志")
        return

    task_received = next((r for r in rows if r.get("event") == "task_received"), None)
    if task_received:
        print(f"任务 {args.task_id} 收到时间: {task_received.get('ts')}")
        print(f"任务子项数量: {task_received.get('launches')}")
        print()

    subtasks: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for r in rows:
        sid = r.get("subtask_id")
        if not sid:
            continue
        info = subtasks[sid]
        evt = r.get("event")
        if evt == "subtask_assigned":
            info["vehicle_id"] = r.get("vehicle_id")
            info["launch_point"] = r.get("launch_point")
            info["fire_time"] = r.get("fire_time")
        elif evt == "plan_execute":
            info[f"phase_{r.get('phase')}"] = {
                "delay_sec": r.get("delay_sec"),
                "start_at": r.get("start_at"),
                "end_at": r.get("end_at"),
            }
        elif evt == "request_depot":
            info["depot"] = r.get("depot")
        elif evt == "request_return":
            info["home"] = r.get("home")
        elif evt == "subtask_done":
            info["done"] = r.get("ts")
        elif evt == "subtask_reset":
            info["reset"] = {"ts": r.get("ts"), "reason": r.get("reason"), "status": r.get("status")}

    for sid in sorted(subtasks):
        info = subtasks[sid]
        print(f"{sid}")
        print(f"  车辆: {info.get('vehicle_id', '-')}")
        print(f"  发射点: {info.get('launch_point', '-')}")
        print(f"  要求发射时刻: {info.get('fire_time', '-')}")
        for phase in ["to_fire", "to_depot", "return_home"]:
            p = info.get(f'phase_{phase}')
            if p:
                print(
                    f"  {phase}: delay={p.get('delay_sec')}s start={p.get('start_at')} end={p.get('end_at')}"
                )
        if "depot" in info:
            print(f"  贮备库: {info['depot']}")
        if "home" in info:
            print(f"  回程终点: {info['home']}")
        if "done" in info:
            print(f"  完成时间: {info['done']}")
        if "reset" in info:
            print(
                f"  回收/失败: {info['reset']['ts']} reason={info['reset']['reason']} status={info['reset']['status']}"
            )
        print()


if __name__ == "__main__":
    main()
