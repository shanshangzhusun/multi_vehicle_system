#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a task package with relative fire times")
    parser.add_argument("--out", default="tasks/generated_task.json")
    parser.add_argument("--task-id", default="task_generated")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--start-after-sec", type=int, default=60)
    parser.add_argument("--interval-sec", type=int, default=20)
    parser.add_argument("--same-fire-time", action="store_true", help="All launches in this task share the same fire time")
    args = parser.parse_args()

    ammo_cycle = ["HE", "AP", "SMOKE"]
    launches = []
    for i in range(args.count):
        fire_after_sec = args.start_after_sec if args.same_fire_time else args.start_after_sec + i * args.interval_sec
        launches.append(
            {
                "ammo_type": ammo_cycle[i % len(ammo_cycle)],
                "fire_after_sec": fire_after_sec,
            }
        )

    obj = {
        "task_id": args.task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dispatch_time": None,
        "launches": launches,
    }
    Path(args.out).write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"generated task => {args.out}")


if __name__ == "__main__":
    main()
