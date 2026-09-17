#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_model_cache_variant(src_path: str, out_path: str, *, enabled: bool) -> str:
    src = Path(src_path)
    if not src.exists():
        return src_path
    dst = Path(out_path)
    model = load_json(src)
    cache_cfg = model.get("cache") if isinstance(model.get("cache"), dict) else {}
    cache_cfg = dict(cache_cfg)
    cache_cfg["enabled"] = bool(enabled)
    cache_cfg.setdefault("length_bin_m", 5.0)
    cache_cfg.setdefault("curvature_bin", 0.0005)
    cache_cfg.setdefault("speed_bin_mps", 0.5)
    model["cache"] = cache_cfg
    model["role"] = "teacher_direct_no_cache" if not enabled else "teacher_cached_lookup"
    write_json(dst, model)
    return dst.as_posix()


def resolve_eta_model_path(deployment: Dict[str, Any]) -> str:
    eta_cfg = deployment.get("eta_time_predictor") if isinstance(deployment.get("eta_time_predictor"), dict) else {}
    mode = str(eta_cfg.get("mode") or "").strip().lower()
    if mode in {"", "fallback", "none", "disabled", "off", "no_model"}:
        if mode:
            return ""
        # No explicit mode keeps old compatibility: use model_path when present.
        # Explicit fallback/off means no neural-network weight file is loaded.
        return str(
            eta_cfg.get("model_path")
            or deployment.get("time_predict_model_path")
            or ""
        ).strip()
    if mode in {"student", "student_model", "runtime", "runtime_student"}:
        return str(
            eta_cfg.get("student_model_path")
            or eta_cfg.get("runtime_model_path")
            or eta_cfg.get("model_path")
            or deployment.get("time_predict_model_path")
            or ""
        ).strip()
    if mode in {"teacher_cached", "teacher_cache", "cached_teacher"}:
        teacher_path = str(
            eta_cfg.get("teacher_cached_model_path")
            or eta_cfg.get("teacher_model_path")
            or eta_cfg.get("model_path")
            or deployment.get("time_predict_model_path")
            or ""
        ).strip()
        if teacher_path:
            out_path = str(eta_cfg.get("teacher_cached_variant_path") or teacher_path)
            return write_model_cache_variant(teacher_path, out_path, enabled=True)
    if mode in {"teacher_direct", "teacher", "direct", "teacher_no_cache"}:
        teacher_path = str(
            eta_cfg.get("teacher_direct_model_path")
            or eta_cfg.get("teacher_model_path")
            or eta_cfg.get("model_path")
            or deployment.get("time_predict_model_path")
            or ""
        ).strip()
        if teacher_path:
            out_path = str(eta_cfg.get("teacher_direct_variant_path") or "models/eta_mlp_teacher_400x300_exit_speed_direct.json")
            return write_model_cache_variant(teacher_path, out_path, enabled=False)
    return str(
        eta_cfg.get("model_path")
        or deployment.get("time_predict_model_path")
        or ""
    ).strip()


def deep_update(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def _load_optional_json(path_value: Any, *, base_dir: Path) -> Dict[str, Any]:
    if path_value in {None, ""}:
        return {}
    path = Path(str(path_value))
    if not path.is_absolute():
        path = base_dir / path
    if not path.exists():
        return {}
    data = load_json(path)
    return data if isinstance(data, dict) else {}


def load_deployment(path: Path) -> Dict[str, Any]:
    deployment = load_json(path)
    base_dir = path.resolve().parent.parent if path.name else Path.cwd()
    theaters = deployment.get("theaters")
    if isinstance(theaters, list):
        merged_theaters = []
        for row in theaters:
            if not isinstance(row, dict):
                merged_theaters.append(row)
                continue
            params = _load_optional_json(row.get("params_file"), base_dir=Path.cwd())
            if params:
                merged = dict(row)
                deep_update(merged, params)
                merged_theaters.append(merged)
            else:
                merged_theaters.append(row)
        deployment["theaters"] = merged_theaters
    return deployment


def deployment_advertise_host(deployment: Dict[str, Any]) -> Optional[str]:
    env_advertise = os.environ.get("MVS_ADVERTISE_HOST", "").strip()
    if env_advertise:
        return env_advertise
    env_local = os.environ.get("MVS_LOCAL_HOST", "").strip()
    if env_local:
        return env_local
    network = deployment.get("network") if isinstance(deployment.get("network"), dict) else {}
    value = str(network.get("advertise_host") or deployment.get("advertise_host") or network.get("local_host") or "").strip()
    return value or None


def deployment_local_host(deployment: Dict[str, Any], default: str = "127.0.0.1") -> str:
    env_local = os.environ.get("MVS_LOCAL_HOST", "").strip()
    if env_local:
        return env_local
    network = deployment.get("network") if isinstance(deployment.get("network"), dict) else {}
    value = str(network.get("local_host") or "").strip()
    return value or default


def deployment_connect_host(deployment: Dict[str, Any], row: Dict[str, Any], default: str = "127.0.0.1") -> str:
    env_advertise = os.environ.get("MVS_ADVERTISE_HOST", "").strip()
    if env_advertise:
        return env_advertise
    env_local = os.environ.get("MVS_LOCAL_HOST", "").strip()
    if env_local:
        return env_local
    network = deployment.get("network") if isinstance(deployment.get("network"), dict) else {}
    value = (
        row.get("advertise_host")
        or row.get("host")
        or network.get("advertise_host")
        or deployment.get("advertise_host")
        or row.get("local_host")
        or network.get("local_host")
        or default
    )
    return str(value).strip()


def vehicle_advertise_host_for_port(
    deployment: Dict[str, Any],
    port: int,
    default: Optional[str],
) -> Optional[str]:
    vehicles = deployment.get("vehicles") if isinstance(deployment.get("vehicles"), dict) else {}
    mappings = vehicles.get("host_mappings") if isinstance(vehicles.get("host_mappings"), list) else []
    for row in mappings:
        if not isinstance(row, dict):
            continue
        try:
            start = int(row.get("start", row.get("port_start")))
            end = int(row.get("end", row.get("port_end", start)))
        except (TypeError, ValueError):
            continue
        host = str(row.get("host") or row.get("advertise_host") or "").strip()
        if host and start <= int(port) <= end:
            return host
    return default


def deployment_model_row(deployment: Dict[str, Any]) -> Dict[str, Any]:
    row = deployment.get("model")
    return row if isinstance(row, dict) else {}


def deployment_vehicle_ports(deployment: Dict[str, Any]) -> List[int]:
    theaters = deployment_theaters(deployment)
    theater_ports: List[int] = []
    for row in theaters:
        theater_ports.extend(theater_vehicle_ports(row))
    if theater_ports:
        return theater_ports
    vehicles = deployment.get("vehicles") if isinstance(deployment.get("vehicles"), dict) else {}
    start = int(vehicles.get("vehicle_start_port", 8414) or 8414)
    count = int(vehicles.get("vehicle_count", vehicles.get("count", 64)) or 64)
    ports = list(range(start, start + count))
    if bool(vehicles.get("enable_extended_128", False)):
        extra_start = int(vehicles.get("extended_vehicle_start_port", 8519) or 8519)
        extra_count = int(vehicles.get("extended_vehicle_count", 64) or 64)
        ports.extend(range(extra_start, extra_start + extra_count))
    return ports


def deployment_theaters(deployment: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = deployment.get("theaters")
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def theater_for_port(theaters: List[Dict[str, Any]], port: int) -> Optional[Dict[str, Any]]:
    for row in theaters:
        if int(port) in set(theater_vehicle_ports(row)):
            return row
    return None


def theater_by_id(theaters: List[Dict[str, Any]], theater_id: str) -> Optional[Dict[str, Any]]:
    target = str(theater_id or "").strip()
    if not target:
        return None
    for row in theaters:
        if str(row.get("id") or "").strip() == target:
            return row
    return None


def theater_bounds_string(row: Dict[str, Any]) -> str:
    bounds = row.get("bounds") or row.get("bbox")
    if isinstance(bounds, str):
        return bounds
    if isinstance(bounds, list) and len(bounds) == 4:
        return ",".join(str(float(v)) for v in bounds)
    return ""


def theater_vehicle_ports(row: Dict[str, Any]) -> List[int]:
    ports: List[int] = []
    ranges = row.get("vehicle_port_ranges")
    if isinstance(ranges, list):
        for item in ranges:
            start = end = None
            if isinstance(item, dict):
                try:
                    start = int(item.get("start", item.get("port_start")))
                    end = int(item.get("end", item.get("port_end", start)))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, (list, tuple)) and item:
                try:
                    start = int(item[0])
                    end = int(item[1] if len(item) > 1 else item[0])
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, str):
                parts = item.replace(":", "-").split("-", 1)
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip()) if len(parts) > 1 else start
                except (TypeError, ValueError):
                    continue
            if start is None or end is None:
                continue
            if end < start:
                start, end = end, start
            ports.extend(range(start, end + 1))
    if ports:
        return sorted(dict.fromkeys(ports))
    start = int(row.get("vehicle_port_start", 0) or 0)
    count = int(row.get("vehicle_port_count", 0) or 0)
    if start <= 0 or count <= 0:
        return []
    return list(range(start, start + count))


def ensure_theater_map_overrides(run_root: Path, deployment: Dict[str, Any]) -> Dict[str, Any]:
    map_cfg = deployment.get("map") if isinstance(deployment.get("map"), dict) else {}
    roads_shp = str(map_cfg.get("roads_shp") or "").strip()
    theaters = deployment.get("theaters")
    if not roads_shp or not isinstance(theaters, list):
        return {"theater_maps": []}
    generated: List[Dict[str, Any]] = []
    for row in theaters:
        if not isinstance(row, dict):
            continue
        theater_id = str(row.get("id") or "").strip()
        bounds = theater_bounds_string(row)
        ports = theater_vehicle_ports(row)
        if not theater_id or not bounds or not ports:
            continue
        out_root = run_root / "theaters" / theater_id
        graph_json = out_root / "data" / "road_graph_shp_demo.json"
        points_json = out_root / "data" / "special_points_shp_demo.json"
        cmd = [
            sys.executable,
            "scripts/build_shp_region_demo.py",
            "--roads-shp",
            roads_shp,
            "--bbox",
            bounds,
            "--out-root",
            str(out_root),
            "--vehicles",
            str(len(ports)),
            "--vehicle-ports",
            ",".join(str(p) for p in ports),
            "--vehicle-port-base",
            str(ports[0] - 1),
            "--hide-point-count",
            str(int(map_cfg.get("hide_point_count", 0) or 0)),
            "--launch-point-count",
            str(int(map_cfg.get("launch_point_count", 0) or 0)),
        ]
        vehicle_tuning = deployment.get("vehicle_tuning") if isinstance(deployment.get("vehicle_tuning"), dict) else {}
        if vehicle_tuning.get("speed_mps") is not None:
            cmd.extend(["--vehicle-speed-mps", str(vehicle_tuning["speed_mps"])])
        for section, args_map in (
            ("hide_strategy", {"trigger_slack_sec": "--hide-trigger-slack-sec", "min_wait_sec": "--hide-min-wait-sec"}),
            (
                "launch_timing",
                {
                    "hot_distance_threshold_m": "--hot-distance-threshold-m",
                    "cold_distance_threshold_m": "--cold-distance-threshold-m",
                    "launch_prepare_sec": "--launch-prepare-sec",
                    "hot_standby_sec": "--hot-standby-sec",
                    "cold_standby_sec": "--cold-standby-sec",
                    "hot_startup_sec": "--hot-startup-sec",
                    "cold_startup_sec": "--cold-startup-sec",
                },
            ),
        ):
            values = deployment.get(section) if isinstance(deployment.get(section), dict) else {}
            for key, arg_name in args_map.items():
                if values.get(key) is not None:
                    cmd.extend([arg_name, str(values[key])])
        subprocess.run(cmd, check=True)
        override = {
            "source_type": "graph_json",
            "graph_json": str(graph_json.as_posix()),
            "points_json": str(points_json.as_posix()),
        }
        row.setdefault("map_override", override)
        row.setdefault("vehicle_map_override", override)
        row.setdefault("scheduler_map_override", override)
        generated.append({"id": theater_id, **override})
    return {"theater_maps": generated}


def graph_nodes(run_root: Path) -> Dict[str, Dict[str, float]]:
    graph_path = run_root / "data" / "road_graph_shp_demo.json"
    obj = load_json(graph_path)
    return {
        str(row["id"]): {"x": float(row["x"]), "y": float(row["y"])}
        for row in obj.get("nodes", [])
    }


def nearest_node(nodes: Dict[str, Dict[str, float]], x: float, y: float) -> str:
    best = ""
    best_d = float("inf")
    for node_id, row in nodes.items():
        d = math.hypot(float(row["x"]) - x, float(row["y"]) - y)
        if d < best_d:
            best = node_id
            best_d = d
    if not best:
        raise RuntimeError("road graph has no nodes; cannot resolve configured coordinate")
    return best


def resolve_node(item: Any, nodes: Dict[str, Dict[str, float]]) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        raise ValueError(f"unsupported point item: {item!r}")
    for key in ("node_id", "home_node", "current_node", "id"):
        if item.get(key):
            return str(item[key])
    if "x" in item and "y" in item:
        return nearest_node(nodes, float(item["x"]), float(item["y"]))
    raise ValueError(f"point item must contain node_id/home_node/current_node or x/y: {item!r}")


def make_scenario(base_config: Path, deployment_config: Path, out_path: Path) -> None:
    scenario = load_json(base_config)
    deployment = load_deployment(deployment_config)

    runtime = deployment.get("runtime") or {}
    map_cfg = deployment.get("map") or {}
    vehicles = deployment.get("vehicles") or {}
    depot = deployment.get("depot") or {}

    if runtime.get("out_root"):
        scenario["out_root"] = runtime["out_root"]
    if map_cfg.get("roads_shp"):
        scenario["roads_shp"] = map_cfg["roads_shp"]
    if map_cfg.get("bbox"):
        scenario["bbox"] = map_cfg["bbox"]
    if map_cfg.get("hide_point_count") is not None:
        scenario["hide_point_count"] = int(map_cfg["hide_point_count"])
    if map_cfg.get("launch_point_count") is not None:
        scenario["launch_point_count"] = int(map_cfg["launch_point_count"])
    if vehicles.get("count"):
        scenario["vehicles"] = int(vehicles["count"])
    vehicle_ports = deployment_vehicle_ports(deployment)
    if vehicle_ports:
        scenario["vehicles"] = len(vehicle_ports)
        scenario["vehicle_ports"] = vehicle_ports
    if vehicles.get("port_base") is not None:
        scenario["vehicle_port_base"] = int(vehicles["port_base"])
    schedulers = deployment.get("schedulers") or {}
    if schedulers.get("listen_port_start") is not None:
        scenario["scheduler_port"] = int(schedulers["listen_port_start"])
    if schedulers.get("dashboard_port_start") is not None:
        scenario["dashboard_port"] = int(schedulers["dashboard_port_start"])
    if depot:
        scenario.setdefault("depot", {})
        if depot.get("capacity") is not None:
            scenario["depot"]["capacity"] = int(depot["capacity"])
        if depot.get("reload_duration_sec") is not None:
            scenario["depot"]["reload_duration_sec"] = float(depot["reload_duration_sec"])
    for section in ("simulation", "vehicle_tuning", "scheduler_tuning", "redundancy", "hide_strategy", "launch_timing"):
        if isinstance(deployment.get(section), dict):
            scenario.setdefault(section, {})
            deep_update(scenario[section], deployment[section])
    if isinstance(deployment.get("waves"), list):
        scenario["waves"] = deployment["waves"]

    write_json(out_path, scenario)
    print(out_path)


def apply_special_points(run_root: Path, deployment: Dict[str, Any]) -> Dict[str, Any]:
    depot_cfg = deployment.get("depot") or {}
    points = depot_cfg.get("points") or []
    if not points:
        return {}
    nodes = graph_nodes(run_root)
    resolved = [resolve_node(item, nodes) for item in points]
    points_path = run_root / "data" / "special_points_shp_demo.json"
    obj = load_json(points_path)
    obj["depots"] = resolved
    write_json(points_path, obj)
    return {"depots": resolved}


def apply_vehicle_starts(run_root: Path, deployment: Dict[str, Any]) -> Dict[str, Any]:
    vehicles_cfg = deployment.get("vehicles") or {}
    home_nodes = vehicles_cfg.get("home_nodes") or []
    explicit_rows = vehicles_cfg.get("vehicle_configs") or []
    model_row = deployment_model_row(deployment)
    fire_model = None
    if model_row:
        fire_model = {
            "local_host": model_row.get("local_host"),
            "host": model_row.get("host"),
            "port": model_row.get("port") or model_row.get("unified_port") or model_row.get("scheduler_port") or model_row.get("fire_port"),
        }
    if not fire_model:
        fire_model = deployment.get("fire_platform", {}).get("model") if isinstance(deployment.get("fire_platform"), dict) else None
    if not fire_model:
        fire_model = deployment.get("fire_platform_model")
    advertise_host = deployment_advertise_host(deployment)
    message_capture_cfg = deployment.get("message_capture") if isinstance(deployment.get("message_capture"), dict) else {}
    message_capture_enabled = bool(message_capture_cfg.get("enabled", True))
    eta_model_path = resolve_eta_model_path(deployment)
    manual_debug_cfg = deployment.get("manual_debug") if isinstance(deployment.get("manual_debug"), dict) else {}
    manual_debug_enabled = bool(manual_debug_cfg.get("enabled", True))
    received_dian_cfg = deployment.get("received_dian_capture") if isinstance(deployment.get("received_dian_capture"), dict) else {}
    received_dian_enabled = bool(received_dian_cfg.get("enabled", True))
    if not home_nodes and not explicit_rows and not isinstance(fire_model, dict) and not advertise_host:
        return {}

    nodes = graph_nodes(run_root) if home_nodes or explicit_rows else {}
    resolved_homes = [resolve_node(item, nodes) for item in home_nodes] if home_nodes else []
    explicit_by_id = {str(row.get("vehicle_id")): row for row in explicit_rows if isinstance(row, dict) and row.get("vehicle_id")}
    vehicles_dir = run_root / "configs" / "vehicles"
    external_vehicle_selection_enabled = bool((deployment.get("model") or {}).get("external_vehicle_selection_enabled", False))
    theaters = deployment_theaters(deployment)
    changed = []
    for idx, path in enumerate(sorted(vehicles_dir.glob("*.json"))):
        cfg = load_json(path)
        listen_port = int(cfg.get("listen_port", 0) or 0)
        theater_row = theater_for_port(theaters, listen_port)
        row = explicit_by_id.get(str(cfg.get("vehicle_id")))
        home = None
        if row:
            home = resolve_node(row, nodes)
        elif resolved_homes:
            home = resolved_homes[idx % len(resolved_homes)]
        if not home:
            pass
        else:
            cfg["home_node"] = home
            cfg["current_node"] = home
            changed.append({"vehicle_id": cfg.get("vehicle_id"), "home_node": home})
        if isinstance(fire_model, dict):
            model_cfg = dict(cfg.get("fire_platform_model", {}))
            connect_host = deployment_connect_host(deployment, fire_model, str(model_cfg.get("host", "127.0.0.1")))
            model_cfg["host"] = connect_host
            if fire_model.get("port") is not None:
                model_cfg["port"] = int(fire_model["port"])
            model_cfg["enabled"] = bool(external_vehicle_selection_enabled)
            model_cfg["target"] = "scheduler_model"
            model_cfg["request_interval_sec"] = float(model_cfg.get("request_interval_sec", 2.0) or 2.0)
            cfg["fire_platform_model"] = model_cfg
            # 候选路径结果输出地址：车辆先发给模型，再由模型转发调度。
            cfg["candidate_result_host"] = connect_host
            if fire_model.get("port") is not None:
                cfg["candidate_result_port"] = int(fire_model["port"])
            cfg.pop("scheduler_host", None)
            cfg.pop("scheduler_port", None)
        vehicle_advertise_host = vehicle_advertise_host_for_port(deployment, listen_port, advertise_host)
        if vehicle_advertise_host:
            cfg["advertise_host"] = vehicle_advertise_host
            if str(vehicle_advertise_host).startswith("127."):
                cfg["listen_host"] = vehicle_advertise_host
            else:
                cfg["listen_host"] = "0.0.0.0"
        fire_node = (deployment.get("fire_platform") or {}).get("node") if isinstance(deployment.get("fire_platform"), dict) else None
        if isinstance(fire_node, dict):
            cfg["vehicle_context_score_submit_host"] = deployment_connect_host(
                deployment,
                fire_node,
                str(cfg.get("vehicle_context_score_submit_host", advertise_host or "127.0.0.1")),
            )
            cfg["vehicle_context_score_submit_port_base"] = int(
                fire_node.get("score_port_base", cfg.get("vehicle_context_score_submit_port_base", 8414)) or 8414
            )
        if theater_row:
            cfg["theater_id"] = str(theater_row.get("id") or "")
            cfg["model_message_type_tag"] = str(theater_row.get("model_message_type_tag") or cfg.get("model_message_type_tag") or "")
            if isinstance(theater_row.get("vehicle_scoring"), dict):
                vehicle_scoring = dict(theater_row.get("vehicle_scoring") or {})
                cfg["vehicle_scoring"] = vehicle_scoring
                if vehicle_scoring.get("health") is not None:
                    cfg["health"] = float(vehicle_scoring.get("health") or 1.0)
            if isinstance(theater_row.get("launch_scoring"), dict):
                cfg["launch_scoring"] = dict(theater_row.get("launch_scoring") or {})
            if theater_row.get("bounds") is not None:
                cfg["theater_bounds"] = theater_row.get("bounds")
            map_override = theater_row.get("vehicle_map_override") or theater_row.get("map_override")
            if isinstance(map_override, dict) and map_override:
                merged_map = dict(cfg.get("map", {}))
                deep_update(merged_map, map_override)
                cfg["map"] = merged_map
        if eta_model_path:
            cfg["time_predict_model_path"] = eta_model_path
        cfg["message_capture"] = {
            "enabled": message_capture_enabled,
            "dir": str((run_root / "message_capture" / "vehicle" / str(cfg["vehicle_id"])).as_posix()),
        }
        cfg["manual_debug"] = {"enabled": manual_debug_enabled}
        cfg["received_dian_capture"] = {
            "enabled": received_dian_enabled,
            "dir": str((run_root / "received_dian").as_posix()),
        }
        write_json(path, cfg)
    return {"vehicles": changed}


def sync_scheduler_vehicle_rows(run_root: Path, deployment: Dict[str, Any]) -> None:
    scheduler_path = run_root / "configs" / "scheduler_debug.json"
    sched = load_json(scheduler_path)
    advertise_host = deployment_advertise_host(deployment)
    message_capture_cfg = deployment.get("message_capture") if isinstance(deployment.get("message_capture"), dict) else {}
    message_capture_enabled = bool(message_capture_cfg.get("enabled", True))
    manual_debug_cfg = deployment.get("manual_debug") if isinstance(deployment.get("manual_debug"), dict) else {}
    manual_debug_enabled = bool(manual_debug_cfg.get("enabled", True))
    theaters = deployment_theaters(deployment)
    vehicles = []
    for path in sorted((run_root / "configs" / "vehicles").glob("*.json")):
        cfg = load_json(path)
        listen_port = int(cfg.get("listen_port", 0) or 0)
        theater_row = theater_for_port(theaters, listen_port)
        vehicles.append(
            {
                "vehicle_id": cfg["vehicle_id"],
                "host": cfg.get("advertise_host", "127.0.0.1"),
                "port": int(cfg.get("advertise_port", cfg["listen_port"])),
                "home_node": cfg["home_node"],
                "ammo_types": cfg.get("ammo_types", ["HE"]),
                "speed_mps": float(cfg.get("speed_mps", 8.5)),
                "kinematics": cfg.get("kinematics", {}),
                "planning": cfg.get("planning", {}),
                "theater_id": str(cfg.get("theater_id") or (theater_row or {}).get("id") or ""),
            }
        )
    if vehicles:
        sched["vehicles"] = vehicles
    if theaters:
        sched["theaters"] = theaters
        by_id = {
            str(row.get("id")): {
                "launch_scoring": row.get("launch_scoring", {}),
                "vehicle_scoring": row.get("vehicle_scoring", {}),
                "depot_scoring": row.get("depot_scoring", {}),
                "redundancy": row.get("redundancy", {}),
            }
            for row in theaters
            if isinstance(row, dict) and row.get("id")
        }
        sched["theater_params"] = by_id

    prewarm = deployment.get("lane_graph_prewarm")
    if prewarm is None:
        prewarm = (deployment.get("schedulers") or {}).get("lane_graph_prewarm")
    if isinstance(prewarm, dict):
        merged_prewarm = dict(sched.get("lane_graph_prewarm", {}))
        merged_prewarm.update(prewarm)
        sched["lane_graph_prewarm"] = merged_prewarm
    else:
        merged_prewarm = dict(sched.get("lane_graph_prewarm", {}))
        merged_prewarm.update(
            {
                "enabled": False,
                "mode": "background",
                "include_hide_points": False,
                "include_return_home": False,
                "max_pairs": 2000,
                "stop_on_work": True,
            }
        )
        sched["lane_graph_prewarm"] = merged_prewarm

    depot = deployment.get("depot") or {}
    if depot.get("capacity") is not None:
        sched["depot_capacity"] = int(depot["capacity"])
    if depot.get("reload_duration_sec") is not None:
        sched["reload_duration_sec"] = float(depot["reload_duration_sec"])
    if depot.get("ammo_capacity") is not None:
        sched["depot_ammo_capacity"] = int(depot["ammo_capacity"])
    redundancy_cfg = dict(sched.get("redundancy", {}))
    global_redundancy = deployment.get("redundancy") if isinstance(deployment.get("redundancy"), dict) else {}
    if global_redundancy:
        redundancy_cfg.update(global_redundancy)
    theater_redundancy = None
    sched_theater = theater_by_id(theaters, str(sched.get("scheduler_theater_id") or ""))
    if isinstance(sched_theater, dict) and isinstance(sched_theater.get("redundancy"), dict):
        theater_redundancy = sched_theater.get("redundancy")
    if theater_redundancy:
        redundancy_cfg.update(theater_redundancy)
    if redundancy_cfg:
        sched["redundancy"] = redundancy_cfg
        sched["disable_redundancy"] = not bool(redundancy_cfg.get("enabled", False))
    two_stage = dict(sched.get("two_stage_depot_assignment", {}))
    assignment_scoring = depot.get("assignment_scoring") if isinstance(depot.get("assignment_scoring"), dict) else {}
    two_stage.update(
        {
            "enabled": bool(depot.get("two_stage_assignment_enabled", True)),
            "depot_host": deployment_connect_host(
                deployment,
                depot.get("software") if isinstance(depot.get("software"), dict) else {},
                str(two_stage.get("depot_host", advertise_host or "127.0.0.1")),
            ),
            "distance_weight_a": float(assignment_scoring.get("distance_weight_a", 1.0)),
            "distance_rank_weight_b": float(assignment_scoring.get("distance_rank_weight_b", 100.0)),
            "load_ratio_weight_c": float(assignment_scoring.get("load_ratio_weight_c", 1000.0)),
            "distance_metric": str(assignment_scoring.get("distance_metric", "euclidean")),
        }
    )
    sched["two_stage_depot_assignment"] = two_stage
    if isinstance(deployment.get("model"), dict) and deployment["model"].get("prelaunch_only") is not None:
        sched["prelaunch_only"] = bool(deployment["model"].get("prelaunch_only"))
    if isinstance(deployment.get("model"), dict) and deployment["model"].get("external_vehicle_selection_enabled") is not None:
        sched["external_vehicle_selection_enabled"] = bool(deployment["model"].get("external_vehicle_selection_enabled"))
    external_conflict = deployment.get("external_conflict_resolution")
    if isinstance(external_conflict, dict):
        merged_external_conflict = dict(sched.get("external_conflict_resolution", {}))
        merged_external_conflict.update(external_conflict)
        sched["external_conflict_resolution"] = merged_external_conflict
    fire_node = (deployment.get("fire_platform") or {}).get("node") if isinstance(deployment.get("fire_platform"), dict) else None
    if isinstance(fire_node, dict):
        scoring_context = dict(sched.get("vehicle_scoring_context", {}))
        scoring_context["enabled"] = bool(scoring_context.get("enabled", True))
        scoring_context["score_submit_host"] = deployment_connect_host(
            deployment,
            fire_node,
            str(scoring_context.get("score_submit_host", advertise_host or "127.0.0.1")),
        )
        ports = deployment_vehicle_ports(deployment)
        scoring_context["score_port_base"] = int(fire_node.get("score_port_base", scoring_context.get("score_port_base", 8000)) or 8000)
        scoring_context["score_port_count"] = len(ports) if ports else int(
            fire_node.get("score_port_count", scoring_context.get("score_port_count", 128)) or 128
        )
        if ports:
            scoring_context["score_ports"] = ports
        sched["vehicle_scoring_context"] = scoring_context
    if isinstance(depot.get("software"), dict):
        platform = dict(sched.get("depot_platform", {}))
        platform["enabled"] = bool(depot["software"].get("enabled", True))
        platform["host"] = deployment_connect_host(deployment, depot["software"], str(platform.get("host", "127.0.0.1")))
        platform["port"] = int(depot["software"].get("port", platform.get("port", 9130)))
        platform["reply_host"] = deployment_connect_host(
            deployment,
            depot["software"],
            str(depot["software"].get("reply_host", platform.get("reply_host", "127.0.0.1"))),
        )
        sched["depot_platform"] = platform
    if advertise_host:
        depot_platform = dict(sched.get("depot_platform", {}))
        if depot_platform:
            depot_platform["reply_host"] = advertise_host
            sched["depot_platform"] = depot_platform
    model_row = deployment_model_row(deployment)
    if model_row:
        model_callback = dict(sched.get("model_callback", {}))
        model_callback["enabled"] = True
        callback_row = dict(model_row)
        if model_row.get("callback_host") is not None:
            callback_row["host"] = model_row.get("callback_host")
        elif model_row.get("reply_host") is not None:
            callback_row["host"] = model_row.get("reply_host")
        model_callback["host"] = deployment_connect_host(
            deployment,
            callback_row,
            str(model_callback.get("host", "127.0.0.1")),
        )
        model_port = (
            model_row.get("callback_port")
            or model_row.get("reply_port")
            or model_row.get("port")
            or model_row.get("unified_port")
            or model_row.get("scheduler_port")
            or model_row.get("fire_port")
        )
        if model_port is not None:
            model_callback["port"] = int(model_port)
        sched["model_callback"] = model_callback
    sched["message_capture"] = {
        "enabled": message_capture_enabled,
        "dir": str((run_root / "message_capture" / "scheduler" / str(sched.get("node_id", "scheduler_001"))).as_posix()),
    }
    sched["manual_debug"] = {"enabled": manual_debug_enabled}
    write_json(scheduler_path, sched)

    theater_common_keys = [
        "theaters",
        "theater_params",
        "lane_graph_prewarm",
        "depot_capacity",
        "reload_duration_sec",
        "depot_ammo_capacity",
        "redundancy",
        "disable_redundancy",
        "two_stage_depot_assignment",
        "prelaunch_only",
        "external_vehicle_selection_enabled",
        "external_conflict_resolution",
        "depot_platform",
        "model_callback",
        "manual_debug",
    ]
    for theater_row in theaters:
        theater_id = str(theater_row.get("id") or "").strip()
        if not theater_id:
            continue
        theater_scheduler_path = run_root / "theaters" / theater_id / "configs" / "scheduler_debug.json"
        if not theater_scheduler_path.exists():
            continue
        theater_sched = load_json(theater_scheduler_path)
        for key in theater_common_keys:
            if key in sched:
                theater_sched[key] = sched[key]
        theater_sched["scheduler_theater_id"] = theater_id
        if isinstance(theater_row.get("launch_scoring"), dict):
            theater_sched["launch_scoring"] = dict(theater_row.get("launch_scoring") or {})
        write_json(theater_scheduler_path, theater_sched)


def write_scheduler_platforms(run_root: Path, deployment: Dict[str, Any], out_path: Optional[Path] = None) -> Path:
    schedulers = deployment.get("schedulers") or {}
    advertise_host = deployment_advertise_host(deployment)
    local_host = deployment_local_host(deployment)
    platforms = schedulers.get("platforms") or []
    theaters = deployment_theaters(deployment)
    def attach_platform_map_override(item: Dict[str, Any]) -> Dict[str, Any]:
        theater_row = theater_by_id(theaters, str(item.get("theater_id") or ""))
        map_override = item.get("scheduler_map_override") or item.get("map_override")
        if not isinstance(map_override, dict) and theater_row:
            map_override = theater_row.get("scheduler_map_override") or theater_row.get("map_override")
        if isinstance(map_override, dict) and map_override:
            item["scheduler_map_override"] = map_override
        return item
    if advertise_host:
        if not platforms:
            platforms = [
                {
                    "host": advertise_host,
                    "listen_host": "0.0.0.0",
                }
            ]
        else:
            normalized = []
            for row in platforms:
                item = dict(row)
                item["host"] = advertise_host
                item.setdefault("listen_host", "0.0.0.0")
                normalized.append(attach_platform_map_override(item))
            platforms = normalized
    elif not platforms:
        platforms = [
            {
                "host": local_host,
                "listen_host": "0.0.0.0",
            }
        ]
    else:
        platforms = [attach_platform_map_override(dict(row)) for row in platforms]
    out = {
        "count": int(schedulers.get("count", 1)),
        "base_config": str((run_root / "configs" / "scheduler_debug.json").as_posix()),
        "out_dir": str((run_root / "configs" / "schedulers").as_posix()),
        "manifest": str((run_root / "configs" / "schedulers_manifest.json").as_posix()),
        "node_id_prefix": str(schedulers.get("node_id_prefix", "scheduler")),
        "listen_port_start": int(schedulers.get("listen_port_start", 9120)),
        "dashboard_port_start": int(schedulers.get("dashboard_port_start", 19120)),
        "event_log_dir": "logs",
        "vehicle_partition": str(schedulers.get("vehicle_partition", "all")),
        "platforms": platforms,
    }
    path = out_path or (run_root / "configs" / "scheduler_platforms.json")
    write_json(path, out)
    return path


def apply_runtime(run_root: Path, deployment_config: Path) -> None:
    deployment = load_deployment(deployment_config)
    applied: Dict[str, Any] = {
        "deployment_config": str(deployment_config),
        "run_root": str(run_root),
    }
    applied.update(ensure_theater_map_overrides(run_root, deployment))
    applied.update(apply_special_points(run_root, deployment))
    applied.update(apply_vehicle_starts(run_root, deployment))
    sync_scheduler_vehicle_rows(run_root, deployment)
    platforms_path = write_scheduler_platforms(run_root, deployment)
    applied["scheduler_platforms_config"] = str(platforms_path)
    write_json(run_root / "configs" / "deployment_applied.json", applied)
    print(run_root / "configs" / "deployment_applied.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a deployment topology JSON to generated MVS runtime files.")
    parser.add_argument("--deployment-config", default="configs/deployment_topology.json")
    parser.add_argument("--base-config", default="configs/shp_four_wave_demo.json")
    parser.add_argument("--run-root", default="result")
    parser.add_argument("--out", default="")
    parser.add_argument("--stage", choices=["scenario", "runtime", "scheduler-platforms"], required=True)
    args = parser.parse_args()

    deployment_path = Path(args.deployment_config)
    if args.stage == "scenario":
        out = Path(args.out or "result/configs/scenario.from_deployment.json")
        make_scenario(Path(args.base_config), deployment_path, out)
    elif args.stage == "runtime":
        apply_runtime(Path(args.run_root), deployment_path)
    elif args.stage == "scheduler-platforms":
        path = write_scheduler_platforms(Path(args.run_root), load_json(deployment_path), Path(args.out) if args.out else None)
        print(path)


if __name__ == "__main__":
    main()
