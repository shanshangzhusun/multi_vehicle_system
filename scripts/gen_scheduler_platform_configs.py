#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def split_vehicles(vehicles: List[dict], count: int, idx: int, mode: str) -> List[dict]:
    if mode == "all":
        return list(vehicles)
    if mode == "round_robin":
        return [v for pos, v in enumerate(vehicles) if pos % count == idx]
    if mode == "contiguous":
        size = (len(vehicles) + count - 1) // count
        return vehicles[idx * size : (idx + 1) * size]
    raise ValueError(f"unknown vehicle_partition: {mode}")


def filter_vehicles_for_platform(vehicles: List[dict], row: Dict[str, Any]) -> List[dict]:
    theater_id = str(row.get("theater_id") or "").strip()
    if theater_id:
        return [v for v in vehicles if str(v.get("theater_id") or "") == theater_id]
    return list(vehicles)


def deep_update(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def theater_by_id(theaters: List[dict], theater_id: str) -> Dict[str, Any]:
    target = str(theater_id or "").strip()
    for row in theaters:
        if str(row.get("id") or "").strip() == target:
            return dict(row)
    return {}


def platform_row(defaults: Dict[str, Any], idx: int) -> Dict[str, Any]:
    platforms = defaults.get("platforms") or []
    if idx < len(platforms):
        row = dict(platforms[idx])
    else:
        row = {}
    prefix = str(defaults.get("node_id_prefix", "scheduler"))
    row.setdefault("node_id", f"{prefix}_{idx + 1:03d}")
    row.setdefault("host", "127.0.0.1")
    row.setdefault("listen_port", int(defaults.get("listen_port_start", 9120)) + idx)
    row.setdefault("dashboard_port", int(defaults.get("dashboard_port_start", 19120)) + idx)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate configs for one or more scheduler platform instances.")
    parser.add_argument("--platforms-config", default="configs/scheduler_platforms.json")
    parser.add_argument("--base-config", default="")
    parser.add_argument("--count", type=int, default=0)
    args = parser.parse_args()

    platform_cfg_path = Path(args.platforms_config)
    platform_cfg = json.loads(platform_cfg_path.read_text(encoding="utf-8"))
    if args.base_config:
        platform_cfg["base_config"] = args.base_config
    if args.count > 0:
        platform_cfg["count"] = args.count

    base_path = Path(str(platform_cfg.get("base_config", "result/configs/scheduler_debug.json")))
    base = json.loads(base_path.read_text(encoding="utf-8"))
    count = max(1, int(platform_cfg.get("count", 1)))
    out_dir = Path(str(platform_cfg.get("out_dir", "result/configs/schedulers")))
    manifest_path = Path(str(platform_cfg.get("manifest", out_dir / "manifest.json")))
    event_log_dir = str(platform_cfg.get("event_log_dir", "logs"))
    partition = str(platform_cfg.get("vehicle_partition", "all"))
    vehicles = list(base.get("vehicles", []))

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"base_config": str(base_path), "vehicle_partition": partition, "schedulers": []}

    for idx in range(count):
        row = platform_row(platform_cfg, idx)
        cfg = dict(base)
        cfg["node_id"] = row["node_id"]
        cfg["listen_host"] = row.get("listen_host", "0.0.0.0")
        cfg["listen_port"] = int(row["listen_port"])
        cfg["dashboard_host"] = row.get("dashboard_host", "0.0.0.0")
        cfg["dashboard_port"] = int(row["dashboard_port"])
        cfg["event_log_path"] = row.get("event_log_path", f"{event_log_dir}/{row['node_id']}_events.jsonl")
        scoped_vehicles = filter_vehicles_for_platform(vehicles, row)
        cfg["vehicles"] = split_vehicles(scoped_vehicles, count, idx, partition)
        if theater_id := str(row.get("theater_id") or "").strip():
            cfg["scheduler_theater_id"] = theater_id
        if model_message_type_tag := str(row.get("model_message_type_tag") or "").strip():
            cfg["model_message_type_tag"] = model_message_type_tag
        theater_row = theater_by_id(list(base.get("theaters", [])), str(row.get("theater_id") or ""))
        if theater_row:
            theater_params = base.get("theater_params") if isinstance(base.get("theater_params"), dict) else {}
            scoped_params = theater_params.get(str(theater_row.get("id") or "")) if isinstance(theater_params, dict) else {}
            if isinstance(scoped_params, dict):
                if isinstance(scoped_params.get("launch_scoring"), dict):
                    cfg["launch_scoring"] = dict(scoped_params.get("launch_scoring") or {})
                if isinstance(scoped_params.get("vehicle_scoring"), dict):
                    cfg["vehicle_scoring"] = dict(scoped_params.get("vehicle_scoring") or {})
                if isinstance(scoped_params.get("redundancy"), dict):
                    cfg["redundancy"] = dict(scoped_params.get("redundancy") or {})
                    cfg["disable_redundancy"] = not bool(cfg["redundancy"].get("enabled", False))
        map_override = row.get("scheduler_map_override") or row.get("map_override")
        if not isinstance(map_override, dict):
            map_override = theater_row.get("scheduler_map_override") or theater_row.get("map_override")
        if isinstance(map_override, dict) and map_override:
            merged_map = dict(cfg.get("map", {}))
            deep_update(merged_map, map_override)
            cfg["map"] = merged_map
        depot_platform = dict(cfg.get("depot_platform", {}))
        if depot_platform:
            depot_platform["reply_host"] = row.get("host", "127.0.0.1")
            cfg["depot_platform"] = depot_platform

        out_path = out_dir / f"{row['node_id']}.json"
        out_path.write_text(json.dumps(cfg, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        manifest["schedulers"].append(
            {
                "node_id": row["node_id"],
                "host": row.get("host", "127.0.0.1"),
                "port": int(row["listen_port"]),
                "dashboard_port": int(row["dashboard_port"]),
                "config": str(out_path),
                "event_log_path": cfg["event_log_path"],
                "vehicle_count": len(cfg.get("vehicles", [])),
            }
        )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {count} scheduler configs under {out_dir}")
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
