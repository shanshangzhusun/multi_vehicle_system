#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def deep_update(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def load_deployment(path: Path) -> Dict[str, Any]:
    deployment = load(path)
    theaters = deployment.get("theaters")
    if isinstance(theaters, list):
        merged = []
        for row in theaters:
            if not isinstance(row, dict):
                merged.append(row)
                continue
            params_file = row.get("params_file")
            params_path = Path(str(params_file)) if params_file not in {None, ""} else None
            if params_path and not params_path.is_absolute():
                params_path = Path.cwd() / params_path
            if params_path and params_path.exists():
                item = dict(row)
                deep_update(item, load(params_path))
                merged.append(item)
            else:
                merged.append(row)
        deployment["theaters"] = merged
    return deployment


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one gateway and per-depot runtime configs")
    parser.add_argument("--deployment-config", required=True)
    parser.add_argument("--run-root", default="result")
    args = parser.parse_args()

    deployment = load_deployment(Path(args.deployment_config))
    run_root = Path(args.run_root)
    depot = deployment.get("depot") if isinstance(deployment.get("depot"), dict) else {}
    network = deployment.get("network") if isinstance(deployment.get("network"), dict) else {}
    model = deployment.get("model") if isinstance(deployment.get("model"), dict) else {}
    message_capture_cfg = deployment.get("message_capture") if isinstance(deployment.get("message_capture"), dict) else {}
    message_capture_enabled = bool(message_capture_cfg.get("enabled", True))
    host = str(network.get("advertise_host") or network.get("local_host") or "127.0.0.1")
    model_host = str(model.get("host") or model.get("advertise_host") or host)
    model_port = int(model.get("port") or model.get("unified_port") or 8888)
    start_port = int(depot.get("start_port", 8601) or 8601)
    count = int(depot.get("count", 24) or 24)
    per_theater = int(depot.get("per_theater_count", 12) or 12)
    gateway_port = int(depot.get("gateway_port", 9191) or 9191)
    capacity = int(depot.get("capacity", 16) or 16)
    reload_sec = float(depot.get("reload_duration_sec", 60.0) or 60.0)
    resource_limit = int(depot.get("resource_limit", depot.get("ammo_capacity", 20)) or 20)
    assignment_scoring = depot.get("assignment_scoring") if isinstance(depot.get("assignment_scoring"), dict) else {}
    theater_rows = deployment.get("theaters") if isinstance(deployment.get("theaters"), list) else []
    groups = []
    cursor = start_port
    for idx, row in enumerate(theater_rows):
        theater_count = int(row.get("depot_count", per_theater) or per_theater)
        groups.append(
            {
                "id": str(row.get("id") or f"theater_{idx+1}"),
                "group": str(row.get("model_message_type_tag") or f"group_{idx+1}"),
                "start": cursor,
                "end": cursor + theater_count - 1,
                "params": row,
            }
        )
        cursor += theater_count
    if not groups:
        groups = [
            {"id": "henan", "group": "26D", "start": start_port, "end": start_port + per_theater - 1, "params": {}},
            {
                "id": "guangdong",
                "group": "27",
                "start": start_port + per_theater,
                "end": start_port + count - 1,
                "params": {},
            },
        ]

    out_dir = run_root / "configs" / "depots"
    for old_path in out_dir.glob("*.json"):
        old_path.unlink()
    generated = []
    for port in range(start_port, start_port + count):
        theater = next(row for row in groups if row["start"] <= port <= row["end"])
        theater_params = theater.get("params") if isinstance(theater.get("params"), dict) else {}
        theater_depot_scoring = (
            theater_params.get("depot_scoring") if isinstance(theater_params.get("depot_scoring"), dict) else {}
        )
        theater_capacity = int(theater_depot_scoring.get("ammo_capacity", depot.get("ammo_capacity", resource_limit)) or resource_limit)
        scheduler_path = run_root / "theaters" / theater["id"] / "configs" / "scheduler_debug.json"
        if not scheduler_path.exists():
            scheduler_path = run_root / "configs" / "scheduler_debug.json"
        scheduler = load(scheduler_path)
        config = {
            "node_id": f"depot_{port}",
            "depot_id": str(port),
            "depot_index": port - theater["start"],
            "theater_id": theater["id"],
            "model_message_type_tag": theater["group"],
            "listen_host": "0.0.0.0",
            "listen_port": port,
            "advertise_host": host,
            "advertise_port": port,
            "transport": scheduler.get("transport", {"type": "tcp"}),
            "map": scheduler["map"],
            "lane_graph_cache_limit": scheduler.get("lane_graph_cache_limit", 32768),
            "depot_capacity": capacity,
            "reload_duration_sec": reload_sec,
            "resource_limit": theater_capacity,
            "ammo_capacity": theater_capacity,
            "assignment_scoring": {
                "distance_weight_a": float(theater_depot_scoring.get("distance_weight_a", assignment_scoring.get("distance_weight_a", 1.0))),
                "distance_rank_weight_b": float(theater_depot_scoring.get("distance_rank_weight_b", assignment_scoring.get("distance_rank_weight_b", 100.0))),
                "load_ratio_weight_c": float(theater_depot_scoring.get("load_ratio_weight_c", assignment_scoring.get("load_ratio_weight_c", 1000.0))),
                "distance_metric": str(assignment_scoring.get("distance_metric", "euclidean")),
            },
            "depot_model": {
                "enabled": False,
                "host": model_host,
                "port": model_port,
                "target": "scheduler_model",
            },
            "event_log_path": f"logs/{port}_events.jsonl",
            "message_capture": {
                "enabled": message_capture_enabled,
                "dir": str((run_root / "message_capture" / "depot" / str(port)).as_posix()),
            },
        }
        path = out_dir / f"{port}.json"
        write(path, config)
        generated.append({"port": port, "group": theater["group"], "config": str(path)})

    gateway = {
        "node_id": "depot_gateway",
        "listen_host": "0.0.0.0",
        "listen_port": gateway_port,
        "model_host": model_host,
        "model_port": model_port,
        "node_host": str((depot.get("node") or {}).get("host") or "192.168.2.12"),
        "event_log_path": "logs/depot_gateway_events.jsonl",
        "message_capture": {
            "enabled": message_capture_enabled,
            "dir": str((run_root / "message_capture" / "depot" / "gateway").as_posix()),
        },
        "port_groups": [
            {"start": row["start"], "end": row["end"], "group": row["group"]}
            for row in groups
        ],
    }
    gateway_path = run_root / "configs" / "depot_gateway.json"
    write(gateway_path, gateway)
    manifest_path = run_root / "configs" / "depots_manifest.json"
    write(
        manifest_path,
        {
            "gateway_config": str(gateway_path),
            "depots_dir": str(out_dir),
            "depots": generated,
        },
    )
    group_summary = " ".join(f"{row['id']}={row['start']}-{row['end']}" for row in groups)
    print(f"wrote {manifest_path} depots={len(generated)} {group_summary}")


if __name__ == "__main__":
    main()
