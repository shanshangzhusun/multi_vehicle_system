#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate N vehicle config files")
    parser.add_argument("--count", type=int, default=128)
    parser.add_argument("--out-dir", default="configs/vehicles")
    parser.add_argument("--candidate-result-host", default="127.0.0.1")
    parser.add_argument("--candidate-result-port", type=int, default=9000)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--start-port", type=int, default=10001)
    parser.add_argument("--graph-json", default="data/road_graph.json")
    parser.add_argument("--home-prefix", default="n")
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    for i in range(1, args.count + 1):
        port = args.start_port + i - 1
        vid = str(port)
        cfg = {
            "vehicle_id": vid,
            "listen_host": args.listen_host,
            "listen_port": port,
            "advertise_host": "127.0.0.1" if args.listen_host == "0.0.0.0" else args.listen_host,
            "advertise_port": port,
            "candidate_result_host": args.candidate_result_host,
            "candidate_result_port": args.candidate_result_port,
            "transport": {
                "type": "tcp",
                "tcp": {
                    "connect_timeout_sec": 1.5,
                    "read_timeout_sec": 2.0,
                    "max_retries": 3,
                    "retry_backoff_sec": 0.15
                },
                "kafka": {
                    "bootstrap_servers": ["127.0.0.1:9092"],
                    "topic_task_package": "mvs.task.package",
                    "topic_vehicle_telemetry": "mvs.vehicle.telemetry",
                    "topic_command_prefix": "mvs.scheduler.command",
                    "consumer_group": "mvs.default",
                    "client_id": "mvs-vehicle"
                }
            },
            "fire_platform_model": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 9140,
                "target": "fire_platform_model",
                "initial_delay_sec": 0.5,
                "request_interval_sec": 0.0,
                "require_ack": True,
                "response_require_ack": False
            },
            "home_node": f"{args.home_prefix}{((i - 1) % 40) + 1}",
            "ammo_types": ["HE", "SMOKE", "AP"],
            "ammo_capacity": 6,
            "speed_mps": 8.0,
            "predict_model_bias": 1.05,
            "realtime_scale": 0.05,
            "map": {
                "source_type": "graph_json",
                "graph_json": args.graph_json,
                "xodr_path": "",
                "shp_path": "",
                "shp_points_path": "",
                "default_lane_width_m": 8.0,
                "default_speed_limit_mps": 8.0,
            }
        }
        (out / f"{vid}.json").write_text(json.dumps(cfg, ensure_ascii=True, indent=2), encoding="utf-8")

    print(f"generated {args.count} configs in {out}")


if __name__ == "__main__":
    main()
