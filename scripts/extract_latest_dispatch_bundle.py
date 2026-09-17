#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any, Optional


def parse_obj(text: str) -> Optional[Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def unwrap_dispatch(obj: Any) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    msg_type = obj.get("msg_type") or obj.get("msgtype")
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if msg_type == "DISPATCH_TRAJECTORY_BUNDLE" or data.get("msg_type") == "DISPATCH_TRAJECTORY_BUNDLE":
        if "data" in data and isinstance(data.get("data"), dict):
            return data["data"]
        return {k: v for k, v in data.items() if k != "msg_type"}
    if "trajectories" in obj:
        return obj
    return None


def latest_bundle_from_log(path: Path) -> Optional[dict]:
    latest: Optional[dict] = None
    latest_task_id: Optional[str] = None
    merged_rows: list[dict] = []
    seen_vehicle_ids: set[str] = set()
    raw_obj_pattern = re.compile(r"obj=(\{.*\})")
    payload_pattern = re.compile(r"payload=(\{.*\})")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "DISPATCH_TRAJECTORY_BUNDLE" not in line and "trajectories" not in line:
            continue
        candidates = []
        for pattern in (raw_obj_pattern, payload_pattern):
            match = pattern.search(line)
            if match:
                candidates.append(match.group(1))
        if line.lstrip().startswith("{"):
            candidates.append(line.strip())
        for text in candidates:
            bundle = unwrap_dispatch(parse_obj(text))
            if bundle is not None:
                latest = bundle
                task_id = str(bundle.get("task_id") or "")
                if task_id:
                    if task_id != latest_task_id:
                        latest_task_id = task_id
                        merged_rows = []
                        seen_vehicle_ids = set()
                    for row in bundle.get("trajectories") or []:
                        if not isinstance(row, dict):
                            continue
                        vehicle_id = str(row.get("vehicle_id") or row.get("port") or "")
                        dedupe_key = vehicle_id or json.dumps(row, ensure_ascii=False, sort_keys=True)
                        if dedupe_key in seen_vehicle_ids:
                            continue
                        seen_vehicle_ids.add(dedupe_key)
                        merged_rows.append(row)
    if latest is None:
        return None
    if latest_task_id:
        return {"task_id": latest_task_id, "trajectories": merged_rows}
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract latest DISPATCH_TRAJECTORY_BUNDLE from debug log")
    parser.add_argument("--log", default=".run/logs/manual_debug.log")
    parser.add_argument("--out", default="result/latest_dispatch_trajectory_bundle.json")
    args = parser.parse_args()

    bundle = latest_bundle_from_log(Path(args.log))
    if bundle is None:
        raise SystemExit(f"no DISPATCH_TRAJECTORY_BUNDLE found in {args.log}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out),
                "task_id": bundle.get("task_id"),
                "trajectory_count": len(bundle.get("trajectories") or []),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
