#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.common.models import parse_iso_time, utc_now_iso
from mvs.common.transport import send_message_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Send task package to scheduler")
    parser.add_argument("--task", required=True, help="task package json file")
    parser.add_argument("--transport", choices=["tcp", "udp", "kafka"], default="tcp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--dispatch-time", default="", help="Override dispatch_time ISO timestamp for simulated runs")
    parser.add_argument("--bootstrap-servers", default="127.0.0.1:9092")
    parser.add_argument("--topic-task-package", default="mvs.task.package")
    args = parser.parse_args()

    payload = json.loads(Path(args.task).read_text(encoding="utf-8"))
    dispatch_time = args.dispatch_time or utc_now_iso()
    payload["dispatch_time"] = dispatch_time
    dispatch_dt = parse_iso_time(dispatch_time)
    for launch in payload.get("launches", []):
        if "fire_time" not in launch and "fire_after_sec" in launch:
            launch["fire_time"] = (dispatch_dt + timedelta(seconds=float(launch["fire_after_sec"]))).isoformat()
    transport_cfg = {
        "type": args.transport,
        "kafka": {
            "bootstrap_servers": [x.strip() for x in args.bootstrap_servers.split(",") if x.strip()],
            "topic_task_package": args.topic_task_package,
        },
    }
    send_message_once(
        sender="upstream",
        msg_type="TASK_PACKAGE",
        target="scheduler",
        payload=payload,
        transport_cfg=transport_cfg,
        addr=(args.host, args.port) if args.transport in {"tcp", "udp"} else None,
        require_ack=False,
    )
    if args.transport == "kafka":
        print(
            f"sent task={payload['task_id']} launches={len(payload.get('launches', []))} "
            f"-> kafka:{args.topic_task_package}"
        )
    else:
        print(f"sent task={payload['task_id']} launches={len(payload.get('launches', []))} -> {args.host}:{args.port}")


if __name__ == "__main__":
    main()
