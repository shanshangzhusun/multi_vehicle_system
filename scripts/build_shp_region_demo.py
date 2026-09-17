#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import shapefile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.scheduler.map_model import RoadGraph


DEFAULT_CLASSES = [
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "residential",
    "service",
    "unclassified",
    "living_street",
]

CLASS_STYLE_WIDTH = {
    "motorway": 18.0,
    "motorway_link": 16.0,
    "trunk": 16.0,
    "trunk_link": 14.0,
    "primary": 14.0,
    "primary_link": 12.0,
    "secondary": 12.0,
    "secondary_link": 10.0,
    "tertiary": 10.0,
    "tertiary_link": 9.0,
    "residential": 8.0,
    "service": 8.0,
    "unclassified": 8.0,
    "living_street": 7.0,
}

CLASS_STYLE_SPEED = {
    "motorway": 24.0,
    "motorway_link": 20.0,
    "trunk": 20.0,
    "trunk_link": 18.0,
    "primary": 18.0,
    "primary_link": 16.0,
    "secondary": 15.0,
    "secondary_link": 14.0,
    "tertiary": 12.0,
    "tertiary_link": 10.0,
    "residential": 9.0,
    "service": 8.0,
    "unclassified": 9.0,
    "living_street": 8.0,
}


def parse_bbox(value: str) -> Tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be min_lon,min_lat,max_lon,max_lat")
    return parts[0], parts[1], parts[2], parts[3]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def boolish(value: object) -> bool:
    return str(value).lower() in {"1", "true", "yes"}


def file_signature(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def build_cache_key(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_task(task_id: str, count: int, start_after: int, interval: int) -> dict:
    ammo_cycle = ["HE", "AP", "SMOKE"]
    return {
        "task_id": task_id,
        "created_at": utc_now_iso(),
        "dispatch_time": None,
        "launches": [
            {
                "ammo_type": ammo_cycle[i % len(ammo_cycle)],
                "fire_after_sec": start_after + i * interval,
            }
            for i in range(count)
        ],
    }


def bbox_intersects(a: Sequence[float], b: Tuple[float, float, float, float]) -> bool:
    a_minx, a_miny, a_maxx, a_maxy = a
    b_minx, b_miny, b_maxx, b_maxy = b
    return not (a_maxx < b_minx or a_minx > b_maxx or a_maxy < b_miny or a_miny > b_maxy)


def project_lonlat(lon: float, lat: float, lon0: float, lat0: float) -> Tuple[float, float]:
    x = (lon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110540.0
    return x, y


def infer_speed_mps(attrs: Dict[str, object], fclass: str) -> float:
    raw = attrs.get("maxspeed")
    if raw not in {None, ""}:
        try:
            value = float(raw)
            return value / 3.6 if value > 30.0 else value
        except Exception:
            pass
    return CLASS_STYLE_SPEED.get(fclass, 10.0)


def coord_node_id(
    graph: RoadGraph,
    node_index: Dict[Tuple[int, int], str],
    x: float,
    y: float,
    lon: float,
    lat: float,
    tol_m: float,
    next_node_idx: List[int],
) -> str:
    key = (round(x / tol_m), round(y / tol_m))
    node_id = node_index.get(key)
    if node_id:
        return node_id
    node_id = f"r_n{next_node_idx[0]}"
    next_node_idx[0] += 1
    node_index[key] = node_id
    graph.add_node(
        node_id,
        round(x, 3),
        round(y, 3),
        kind="road",
        lon=round(lon, 8),
        lat=round(lat, 8),
        alt=0.0,
    )
    return node_id


def build_graph_from_shp(
    roads_shp: Path,
    bbox_lonlat: Tuple[float, float, float, float],
    include_classes: set[str],
    node_snap_tol_m: float,
) -> RoadGraph:
    # 任务书“地图路网输入”：从道路 SHP 生成统一路网图。
    # include_classes 控制是否纳入小路，node_snap_tol_m 控制相近端点吸附，直接影响点位投影精度。
    reader = shapefile.Reader(str(roads_shp))
    fields = [f[0] for f in reader.fields[1:]]
    lon0 = (bbox_lonlat[0] + bbox_lonlat[2]) / 2.0
    lat0 = (bbox_lonlat[1] + bbox_lonlat[3]) / 2.0
    graph = RoadGraph()
    node_index: Dict[Tuple[int, int], str] = {}
    next_node_idx = [1]
    next_edge_idx = 1

    for sr in reader.iterShapeRecords(fields=["fclass", "maxspeed", "oneway"]):
        shape = sr.shape
        if not shape.points:
            continue
        if not bbox_intersects(shape.bbox, bbox_lonlat):
            continue

        fclass = sr.record[0] or ""
        if include_classes and fclass not in include_classes:
            continue

        attrs = {
            "fclass": sr.record[0],
            "maxspeed": sr.record[1],
            "oneway": sr.record[2],
        }
        lanes_forward = 1
        lanes_backward = 0 if str(attrs.get("oneway", "")).lower() in {"1", "true", "yes"} else 1
        width = CLASS_STYLE_WIDTH.get(fclass, 8.0)
        speed_limit_mps = infer_speed_mps(attrs, fclass)

        parts = list(shape.parts) + [len(shape.points)]
        for part_idx in range(len(parts) - 1):
            pts = shape.points[parts[part_idx] : parts[part_idx + 1]]
            if len(pts) < 2:
                continue
            prev_node = None
            prev_xy = None
            prev_lonlat = None
            for lon, lat in pts:
                lon_f = float(lon)
                lat_f = float(lat)
                x, y = project_lonlat(lon_f, lat_f, lon0, lat0)
                cur_node = coord_node_id(graph, node_index, x, y, lon_f, lat_f, node_snap_tol_m, next_node_idx)
                if prev_node is not None and prev_xy is not None and prev_lonlat is not None:
                    px, py = prev_xy
                    prev_lon, prev_lat = prev_lonlat
                    dist = math.hypot(x - px, y - py)
                    if dist >= 1.0:
                        graph.add_bidirectional_edge(
                            prev_node,
                            cur_node,
                            dist,
                            edge_id=f"r_e{next_edge_idx}",
                            width=width,
                            lanes_forward=lanes_forward,
                            lanes_backward=lanes_backward,
                            speed_limit_mps=speed_limit_mps,
                            curvature=0.0,
                            geometry=[
                                {
                                    "x": round(px, 3),
                                    "y": round(py, 3),
                                    "lon": round(prev_lon, 8),
                                    "lat": round(prev_lat, 8),
                                    "alt": 0.0,
                                },
                                {
                                    "x": round(x, 3),
                                    "y": round(y, 3),
                                    "lon": round(lon_f, 8),
                                    "lat": round(lat_f, 8),
                                    "alt": 0.0,
                                },
                            ],
                        )
                        next_edge_idx += 1
                prev_node = cur_node
                prev_xy = (x, y)
                prev_lonlat = (lon_f, lat_f)
    return graph


def largest_component(graph: RoadGraph) -> set[str]:
    seen: set[str] = set()
    best: set[str] = set()
    for node_id in graph.nodes:
        if node_id in seen:
            continue
        comp: set[str] = set()
        q = deque([node_id])
        seen.add(node_id)
        while q:
            cur = q.popleft()
            comp.add(cur)
            for nb, _ in graph.edges.get(cur, []):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        if len(comp) > len(best):
            best = comp
    return best


def prune_to_component(graph: RoadGraph, keep_nodes: set[str]) -> RoadGraph:
    out = RoadGraph()
    for node_id in keep_nodes:
        n = graph.nodes[node_id]
        out.add_node(node_id, n.x, n.y, n.kind, lon=n.lon, lat=n.lat, alt=n.alt)
    seen_edges: set[tuple[str, str]] = set()
    for a in keep_nodes:
        for b, _ in graph.edges.get(a, []):
            if b not in keep_nodes:
                continue
            key = tuple(sorted((a, b)))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            meta = graph.get_edge_meta(a, b)
            cost = graph.get_edge_cost(a, b)
            if meta is None or cost is None:
                continue
            out.add_bidirectional_edge(
                a,
                b,
                cost,
                edge_id=meta.edge_id,
                width=meta.width,
                lanes_forward=meta.lanes_forward,
                lanes_backward=meta.lanes_backward,
                speed_limit_mps=meta.speed_limit_mps,
                curvature=meta.curvature,
                geometry=list(meta.geometry),
                geom_from=meta.geom_from,
                geom_to=meta.geom_to,
            )
    return out


def oriented_geometry(graph: RoadGraph, a: str, b: str) -> List[Dict[str, float]]:
    meta = graph.get_edge_meta(a, b)
    if meta is None:
        na = graph.nodes[a]
        nb = graph.nodes[b]
        return [{"x": round(na.x, 3), "y": round(na.y, 3)}, {"x": round(nb.x, 3), "y": round(nb.y, 3)}]
    if meta.geometry:
        if meta.geom_from == a and meta.geom_to == b:
            return list(meta.geometry)
        return list(reversed(meta.geometry))
    na = graph.nodes[a]
    nb = graph.nodes[b]
    return [{"x": round(na.x, 3), "y": round(na.y, 3)}, {"x": round(nb.x, 3), "y": round(nb.y, 3)}]


def compatible_for_compaction(meta_a, meta_b) -> bool:
    return (
        abs(meta_a.width - meta_b.width) <= 0.5
        and meta_a.lanes_forward == meta_b.lanes_forward
        and meta_a.lanes_backward == meta_b.lanes_backward
        and abs(meta_a.speed_limit_mps - meta_b.speed_limit_mps) <= 1.0
    )


def compact_linear_graph(graph: RoadGraph) -> RoadGraph:
    degree = {nid: len(graph.edges.get(nid, [])) for nid in graph.nodes}
    keep_nodes = {
        nid
        for nid, node in graph.nodes.items()
        if node.kind != "road" or degree.get(nid, 0) != 2
    }
    if len(keep_nodes) < 2:
        return graph

    out = RoadGraph()
    for nid in keep_nodes:
        node = graph.nodes[nid]
        out.add_node(nid, node.x, node.y, node.kind, lon=node.lon, lat=node.lat, alt=node.alt)

    visited: set[tuple[str, str]] = set()
    compact_idx = 1

    for start in list(keep_nodes):
        for nb, _ in graph.edges.get(start, []):
            edge_key = tuple(sorted((start, nb)))
            if edge_key in visited:
                continue

            path = [start, nb]
            metas = []
            total_cost = 0.0
            prev = start
            cur = nb

            while True:
                meta = graph.get_edge_meta(prev, cur)
                edge_cost = graph.get_edge_cost(prev, cur)
                if meta is None or edge_cost is None:
                    break
                metas.append(meta)
                total_cost += edge_cost
                visited.add(tuple(sorted((prev, cur))))

                if cur in keep_nodes:
                    break

                next_nodes = [cand for cand, _ in graph.edges.get(cur, []) if cand != prev]
                if len(next_nodes) != 1:
                    break
                nxt = next_nodes[0]
                next_meta = graph.get_edge_meta(cur, nxt)
                if next_meta is None or not compatible_for_compaction(meta, next_meta):
                    break
                path.append(nxt)
                prev, cur = cur, nxt

            end = path[-1]
            if end not in out.nodes:
                node = graph.nodes[end]
                out.add_node(end, node.x, node.y, node.kind, lon=node.lon, lat=node.lat, alt=node.alt)
            if start == end or not metas:
                continue

            geometry: List[Dict[str, float]] = []
            for i in range(len(path) - 1):
                seg = oriented_geometry(graph, path[i], path[i + 1])
                if geometry and seg and geometry[-1] == seg[0]:
                    geometry.extend(seg[1:])
                else:
                    geometry.extend(seg)

            first = metas[0]
            out.add_bidirectional_edge(
                start,
                end,
                total_cost,
                edge_id=f"cmp_e{compact_idx}",
                width=first.width,
                lanes_forward=first.lanes_forward,
                lanes_backward=first.lanes_backward,
                speed_limit_mps=first.speed_limit_mps,
                curvature=max(m.curvature for m in metas),
                geometry=geometry,
                geom_from=start,
                geom_to=end,
            )
            compact_idx += 1

    return out


def graph_bounds(graph: RoadGraph) -> Tuple[float, float, float, float]:
    xs = [n.x for n in graph.nodes.values()]
    ys = [n.y for n in graph.nodes.values()]
    return min(xs), min(ys), max(xs), max(ys)


def nearest_node_to_xy(graph: RoadGraph, x: float, y: float, candidates: Optional[Iterable[str]] = None) -> str:
    best = None
    best_d = float("inf")
    node_ids = candidates if candidates is not None else graph.nodes.keys()
    for node_id in node_ids:
        n = graph.nodes[node_id]
        d = math.hypot(n.x - x, n.y - y)
        if d < best_d:
            best_d = d
            best = node_id
    if best is None:
        raise RuntimeError("no node available")
    return best


def edge_midpoint(meta) -> Tuple[float, float]:
    if meta.geometry:
        pts = meta.geometry
        mid = pts[len(pts) // 2]
        return float(mid["x"]), float(mid["y"])
    raise RuntimeError("edge geometry missing")


def pick_edge_points(
    graph: RoadGraph,
    count: int,
    x_range: Tuple[float, float],
    y_range: Tuple[float, float],
    min_spacing_m: float,
    prefix: str,
    forbidden_edge_ids: Optional[set[str]] = None,
) -> List[dict]:
    candidates = []
    blocked = forbidden_edge_ids or set()
    minx, maxx = x_range
    miny, maxy = y_range
    for meta in graph.edge_meta.values():
        if meta.edge_id in blocked:
            continue
        mx, my = edge_midpoint(meta)
        if mx < minx or mx > maxx or my < miny or my > maxy:
            continue
        candidates.append((meta.cost, meta.edge_id, mx, my))
    candidates.sort(reverse=True)
    chosen: List[tuple[float, float, str]] = []
    points: List[dict] = []
    for _, edge_id, mx, my in candidates:
        if any(math.hypot(mx - cx, my - cy) < min_spacing_m for cx, cy, _ in chosen):
            continue
        chosen.append((mx, my, edge_id))
        points.append({"id": f"{prefix}_{len(points)+1:03d}", "edge_id": edge_id, "ratio": 0.5})
        if len(points) >= count:
            break
    return points


def pick_demo_points(graph: RoadGraph, hide_point_count: int = 0, launch_point_count: int = 0) -> dict:
    minx, miny, maxx, maxy = graph_bounds(graph)
    dx = maxx - minx
    dy = maxy - miny

    depot_targets = [
        (minx + 0.05 * dx, miny + 0.05 * dy),
        (maxx - 0.05 * dx, miny + 0.05 * dy),
        (minx + 0.05 * dx, maxy - 0.05 * dy),
        (maxx - 0.05 * dx, maxy - 0.05 * dy),
    ]
    depots: List[str] = []
    for tx, ty in depot_targets:
        node_id = nearest_node_to_xy(graph, tx, ty)
        if node_id not in depots:
            depots.append(node_id)

    hide_points = pick_edge_points(
        graph,
        count=max(0, int(hide_point_count)),
        x_range=(minx + 0.20 * dx, minx + 0.55 * dx),
        y_range=(miny + 0.20 * dy, maxy - 0.15 * dy),
        min_spacing_m=320.0,
        prefix="hide",
    )
    hide_edge_ids = {p["edge_id"] for p in hide_points}
    launch_points = pick_edge_points(
        graph,
        count=max(0, int(launch_point_count)),
        x_range=(minx + 0.38 * dx, maxx - 0.10 * dx),
        y_range=(miny + 0.18 * dy, maxy - 0.18 * dy),
        min_spacing_m=320.0,
        prefix="launch",
        forbidden_edge_ids=hide_edge_ids,
    )
    return {
        "hide_points": hide_points,
        "launch_points": launch_points,
        "depots": depots,
    }


def choose_vehicle_homes(graph: RoadGraph, count: int) -> List[str]:
    minx, miny, maxx, maxy = graph_bounds(graph)
    cols = max(2, math.ceil(math.sqrt(count)))
    rows = max(2, math.ceil(count / cols))
    dx = maxx - minx
    dy = maxy - miny

    targets: List[Tuple[float, float]] = []
    for r in range(rows):
        for c in range(cols):
            tx = minx + dx * ((c + 0.5) / cols)
            ty = miny + dy * ((r + 0.5) / rows)
            targets.append((tx, ty))

    graph_node_ids = list(graph.nodes.keys())
    picked: List[str] = []
    for tx, ty in targets:
        node_id = nearest_node_to_xy(graph, tx, ty)
        if node_id not in picked:
            picked.append(node_id)
        if len(picked) >= count:
            return picked

    # Fallback: greedily pick nodes far from already chosen homes.
    remaining = [nid for nid in graph_node_ids if nid not in picked]
    while len(picked) < count and remaining:
        if not picked:
            picked.append(remaining.pop(0))
            continue
        best_nid = None
        best_score = -1.0
        for nid in remaining:
            n = graph.nodes[nid]
            score = min(
                math.hypot(n.x - graph.nodes[pid].x, n.y - graph.nodes[pid].y)
                for pid in picked
            )
            if score > best_score:
                best_score = score
                best_nid = nid
        if best_nid is None:
            break
        picked.append(best_nid)
        remaining.remove(best_nid)
    return picked


def build_fire_zone_cfg(
    graph: RoadGraph,
    enabled: bool,
    axis: str,
    side: str,
    bonus_score: float,
) -> dict:
    minx, miny, maxx, maxy = graph_bounds(graph)
    axis = axis.lower()
    side = side.lower()
    if axis not in {"x", "y"}:
        axis = "x"
    if side not in {"east", "west", "north", "south"}:
        side = "east"

    if axis == "x":
        mid = (minx + maxx) / 2.0
        if side == "west":
            bbox = [minx, miny, mid, maxy]
        else:
            bbox = [mid, miny, maxx, maxy]
    else:
        mid = (miny + maxy) / 2.0
        if side == "south":
            bbox = [minx, miny, maxx, mid]
        else:
            bbox = [minx, mid, maxx, maxy]

    return {
        "enabled": bool(enabled),
        "mode": "half_map",
        "axis": axis,
        "side": side,
        "bbox": [round(v, 3) for v in bbox],
        "bonus_score": float(bonus_score),
        "description": f"half_map_{side}",
    }


def save_graph_json(graph: RoadGraph, out_path: Path) -> None:
    out = {
        "nodes": [
            {
                "id": n.node_id,
                "x": n.x,
                "y": n.y,
                "kind": n.kind,
                "lon": n.lon,
                "lat": n.lat,
                "alt": n.alt,
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "id": meta.edge_id,
                "from": meta.src,
                "to": meta.dst,
                "geom_from": meta.geom_from,
                "geom_to": meta.geom_to,
                "cost": meta.cost,
                "width": meta.width,
                "lanes_forward": meta.lanes_forward,
                "lanes_backward": meta.lanes_backward,
                "speed_limit_mps": meta.speed_limit_mps,
                "curvature": meta.curvature,
                "geometry": meta.geometry,
            }
            for meta in graph.edge_meta.values()
        ],
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def build_scheduler_and_vehicle_configs(
    out_root: Path,
    graph_json: Path,
    points_json: Path,
    vehicle_homes: List[str],
    vehicles: int,
    scheduler_port: int,
    dashboard_port: int,
    vehicle_port_base: int,
    vehicle_ports: Optional[List[int]],
    fire_zone_cfg: dict,
    scheduler_tick_sec: float,
    heartbeat_timeout_sec: float,
    proposal_timeout_sec: float,
    max_proposal_retries: int,
    reservation_slot_sec: int,
    reservation_delay_step_sec: float,
    reservation_max_delay_sec: float,
    reservation_reserve_wait_nodes: bool,
    reservation_retention_sec: float,
    reservation_deadlock_backoff_sec: float,
    vehicle_realtime_scale: float,
    vehicle_speed_mps: float,
    simulation_mode: str,
    depot_capacity: int,
    reload_duration_sec: float,
    hide_strategy_cfg: dict,
    launch_timing_cfg: dict,
    redundancy_cfg: dict,
    max_route_candidates: int = 8,
    max_route_candidates_real: int = 8,
    max_route_candidates_reserved: int = 8,
    max_route_candidates_redundant: int = 4,
    max_active_real_planning_subtasks: int = 0,
    max_active_redundant_planning_subtasks: int = 0,
    max_active_real_planning_per_task: int = 0,
    max_active_redundant_planning_per_task: int = 0,
    max_active_real_task_groups: int = 0,
    max_active_redundant_task_groups: int = 0,
    vehicle_platform_score_enabled: bool = True,
    vehicle_platform_score_request_limit: int = 24,
    project_book_enabled: bool = True,
    depot_book_enabled: bool = True,
) -> None:
    # 任务书“参数配置/默认车辆参数/贮备库容量”：prepare 阶段把这些参数固化到运行配置。
    # 后续 scheduler、vehicle、depot 都从 result/configs 读取，服务器只需要改 deployment 配置再 prepare。
    cfg_dir = out_root / "configs"
    vehicles_dir = cfg_dir / "vehicles"
    data_dir = out_root / "data"
    tasks_dir = out_root / "tasks"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    vehicles_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    for old_vehicle_cfg in vehicles_dir.glob("*.json"):
        old_vehicle_cfg.unlink()

    task1 = build_task("task_shp_beijing_001", count=8, start_after=35, interval=12)
    task2 = build_task("task_shp_beijing_002", count=6, start_after=95, interval=15)
    (tasks_dir / "task_shp_beijing_001.json").write_text(json.dumps(task1, ensure_ascii=True, indent=2), encoding="utf-8")
    (tasks_dir / "task_shp_beijing_002.json").write_text(json.dumps(task2, ensure_ascii=True, indent=2), encoding="utf-8")

    vehicles_cfg = []
    ports = list(vehicle_ports or [])
    if not ports:
        ports = [vehicle_port_base + i for i in range(1, vehicles + 1)]
    vehicles = len(ports)
    for i, port in enumerate(ports, start=1):
        home = vehicle_homes[(i - 1) % len(vehicle_homes)]
        vid = str(port)
        ammo = ["HE", "AP", "SMOKE"] if i % 2 else ["HE", "AP"]
        kinematics = {
            "width_m": round(2.7 + (i % 3) * 0.12, 2),
            "length_m": round(7.2 + (i % 4) * 0.35, 2),
            "wheelbase_m": round(4.1 + (i % 3) * 0.18, 2),
            "min_turn_radius_m": round(11.5 + (i % 4) * 0.9, 2),
            "max_steer_deg": 28.0,
            "max_turn_angle_deg": 100.0 if i % 2 else 108.0,
            "min_clearance_m": 0.8,
            "turn_penalty": round(9.0 + (i % 3) * 1.5, 2),
            "max_edge_curvature": round(1.0 / (11.5 + (i % 4) * 0.9), 5),
            "max_speed_mps": round(float(vehicle_speed_mps), 3),
        }
        planning = {
            "algorithm": "hybrid_a_star",
            "step_m": 4.0,
            "yaw_resolution_deg": 10.0,
            "position_resolution_m": 4.0,
            "max_steer_deg": 28.0,
            "steering_samples": 7,
            "goal_pos_tolerance_m": 12.0,
            "goal_yaw_tolerance_deg": 35.0,
            "reverse_enabled": False,
            "steering_change_penalty": 1.2,
            "gear_switch_penalty": 8.0,
            "corridor_margin_m": 3.5,
            "max_expansions": 25000,
            "max_hide_candidates": 2,
        }
        vehicles_cfg.append(
            {
                "vehicle_id": vid,
                "host": "127.0.0.1",
                "port": port,
                "home_node": home,
                "ammo_types": ammo,
                "speed_mps": round(float(vehicle_speed_mps), 3),
                "kinematics": kinematics,
                "planning": planning,
            }
        )
        vehicle_cfg = {
            "vehicle_id": vid,
            "listen_host": "0.0.0.0",
            "listen_port": port,
            "advertise_host": "127.0.0.1",
            "advertise_port": port,
            "candidate_result_host": "127.0.0.1",
            "candidate_result_port": scheduler_port,
            "transport": {
                "type": "tcp",
                "tcp": {
                    "connect_timeout_sec": 1.5,
                    "read_timeout_sec": 2.0,
                    "max_retries": 3,
                    "retry_backoff_sec": 0.15,
                },
                "kafka": {
                    "bootstrap_servers": ["127.0.0.1:9092"],
                    "topic_task_package": "mvs.task.package",
                    "topic_vehicle_telemetry": "mvs.vehicle.telemetry",
                    "topic_command_prefix": "mvs.scheduler.command",
                    "consumer_group": "mvs.default",
                    "client_id": "mvs-vehicle",
                },
            },
            "lane_graph_cache_limit": 32768,
            "lane_graph_prewarm": {
                "enabled": False,
                "mode": "background",
                "include_hide_points": True,
            },
            "fire_platform_model": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 9140,
                "target": "fire_platform_model",
                "initial_delay_sec": 0.5,
                "request_interval_sec": 0.0,
                "require_ack": True,
                "response_require_ack": False,
            },
            "home_node": home,
            "ammo_types": ammo,
            "ammo_capacity": 6,
            "speed_mps": round(float(vehicle_speed_mps), 3),
            "kinematics": kinematics,
            "planning": planning,
            "predict_model_bias": 1.03 + (i % 4) * 0.01,
            "time_predict_model_path": "",
            "realtime_scale": vehicle_realtime_scale,
            "simulation_mode": simulation_mode,
            "reload_duration_sec": reload_duration_sec,
            "hide_strategy": dict(hide_strategy_cfg),
            "launch_timing": dict(launch_timing_cfg),
            "map": {
                "source_type": "graph_json",
                "graph_json": str(graph_json.as_posix()),
                "points_json": str(points_json.as_posix()),
                "xodr_path": "",
                "shp_path": "",
                "shp_points_path": "",
                "default_lane_width_m": 8.0,
                "default_speed_limit_mps": 10.0,
            },
        }
        (vehicles_dir / f"{vid}.json").write_text(json.dumps(vehicle_cfg, ensure_ascii=True, indent=2), encoding="utf-8")

    sched_cfg = {
        "node_id": "scheduler_shp_demo",
        "listen_host": "0.0.0.0",
        "listen_port": scheduler_port,
        "transport": {
            "type": "tcp",
            "tcp": {
                "connect_timeout_sec": 1.5,
                "read_timeout_sec": 2.0,
                "max_retries": 3,
                "retry_backoff_sec": 0.15,
            },
            "kafka": {
                "bootstrap_servers": ["127.0.0.1:9092"],
                "topic_task_package": "mvs.task.package",
                "topic_vehicle_telemetry": "mvs.vehicle.telemetry",
                "topic_command_prefix": "mvs.scheduler.command",
                "consumer_group": "mvs.default",
                "client_id": "mvs-scheduler",
            },
        },
        "lane_graph_cache_limit": 65536,
        "lane_graph_prewarm": {
            "enabled": False,
            "mode": "background",
            "include_hide_points": False,
            "include_return_home": False,
            "max_pairs": 2000,
            "stop_on_work": True,
        },
        "dashboard_host": "0.0.0.0",
        "dashboard_port": dashboard_port,
        "tick_sec": scheduler_tick_sec,
        "heartbeat_timeout_sec": heartbeat_timeout_sec,
        "proposal_timeout_sec": proposal_timeout_sec,
        "max_proposal_retries": max_proposal_retries,
        "reservation_slot_sec": reservation_slot_sec,
        "reservation_delay_step_sec": reservation_delay_step_sec,
        "reservation_max_delay_sec": reservation_max_delay_sec,
        "reservation_reserve_wait_nodes": bool(reservation_reserve_wait_nodes),
        "reservation_retention_sec": reservation_retention_sec,
        "reservation_deadlock_backoff_sec": reservation_deadlock_backoff_sec,
        "depot_capacity": depot_capacity,
        "reload_duration_sec": reload_duration_sec,
        "depot_platform": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 9130,
            "reply_host": "127.0.0.1",
            "assignment_timeout_sec": 4.0,
            "connect_timeout_sec": 0.35,
            "ack_timeout_sec": 0.8,
            "max_failures": 3,
        },
        "redundancy": dict(redundancy_cfg),
        "launch_timing": dict(launch_timing_cfg),
        "allowed_ammo_types": ["HE", "AP", "SMOKE"],
        "max_task_launches": 128,
        "reject_past_fire_time": False,
        "past_time_grace_sec": 30.0,
        "map": {
            "source_type": "graph_json",
            "graph_json": str(graph_json.as_posix()),
            "points_json": str(points_json.as_posix()),
            "xodr_path": "",
            "shp_path": "",
            "shp_points_path": "",
            "default_lane_width_m": 8.0,
            "default_speed_limit_mps": 10.0,
        },
        "assignment_scoring": {
            "distance_weight": 35.0,
            "travel_time_weight": 40.0,
            "lateness_penalty_weight": 1.2,
            "fire_zone_bonus": float(fire_zone_cfg.get("bonus_score", 25.0)),
            "vehicle_platform_score": {
                "enabled": bool(vehicle_platform_score_enabled),
                "request_limit_per_subtask": max(0, int(vehicle_platform_score_request_limit)),
            },
            "project_book": {
                "enabled": bool(project_book_enabled),
                "w_distance": 1.0,
                "w_hide": 0.6,
                "w_support": 1.2,
                "support_radius_m": 5000.0,
                "depot_score_default": 80.0,
                "count_weight": 0.4,
                "mean_weight": 0.35,
                "route_match_weight": 0.25,
                "max_nearby_depots": 3,
                "decision_weight": 4.0,
            },
            "depot_book": {
                "enabled": bool(depot_book_enabled),
                "busy_alpha": 1.0,
                "prep_beta": 1.0,
                "time_nearest_vehicle_k": 3,
                "min_time_ratio": 0.001,
            },
            "max_route_candidates": int(max_route_candidates),
            "max_route_candidates_real": int(max_route_candidates_real),
            "max_route_candidates_reserved": int(max_route_candidates_reserved),
            "max_route_candidates_redundant": int(max_route_candidates_redundant),
            "max_active_real_planning_subtasks": int(max_active_real_planning_subtasks),
            "max_active_redundant_planning_subtasks": int(max_active_redundant_planning_subtasks),
            "max_active_real_planning_per_task": int(max_active_real_planning_per_task),
            "max_active_redundant_planning_per_task": int(max_active_redundant_planning_per_task),
            "max_active_real_task_groups": int(max_active_real_task_groups),
            "max_active_redundant_task_groups": int(max_active_redundant_task_groups),
            "max_lateness_sec": 5.0,
            "fire_window_grace_sec": 15.0,
            "fire_window_grace_after_resets": 2,
            "max_assignment_resets": 8,
            "to_fire_deadlock_bypass_after_resets": 0,
            "to_fire_local_deadlock_retries": 1,
            "to_fire_local_deadlock_backoff_sec": 0.5,
            "max_no_candidate_ticks": 32,
        },
        "fire_zone": fire_zone_cfg,
        "launch_capability": {},
        "vehicles": vehicles_cfg,
    }
    (cfg_dir / "scheduler_debug.json").write_text(json.dumps(sched_cfg, ensure_ascii=True, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an MVS demo root from a clipped SHP road region.")
    parser.add_argument(
        "--roads-shp",
        default="luwang_data/china-240101-free.shp/gis_osm_roads_free_1.shp",
        help="OSM roads shapefile path",
    )
    parser.add_argument(
        "--bbox",
        default="116.24,39.84,116.43,39.97",
        help="Region bbox: min_lon,min_lat,max_lon,max_lat",
    )
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES), help="Comma-separated road classes")
    parser.add_argument("--out-root", default="shp_demo_beijing")
    parser.add_argument("--vehicles", type=int, default=8)
    parser.add_argument("--scheduler-port", type=int, default=9120)
    parser.add_argument("--dashboard-port", type=int, default=19120)
    parser.add_argument("--vehicle-port-base", type=int, default=12100)
    parser.add_argument("--vehicle-ports", default="", help="Comma-separated explicit vehicle listen ports")
    parser.add_argument("--hide-point-count", type=int, default=0)
    parser.add_argument("--launch-point-count", type=int, default=0)
    parser.add_argument("--node-snap-tol-m", type=float, default=6.0)
    parser.add_argument("--fire-zone-enabled", default="true")
    parser.add_argument("--fire-zone-axis", default="x")
    parser.add_argument("--fire-zone-side", default="east")
    parser.add_argument("--fire-zone-bonus", type=float, default=25.0)
    parser.add_argument("--scheduler-tick-sec", type=float, default=0.4)
    parser.add_argument("--heartbeat-timeout-sec", type=float, default=5.0)
    parser.add_argument("--proposal-timeout-sec", type=float, default=4.0)
    parser.add_argument("--max-proposal-retries", type=int, default=2)
    parser.add_argument("--reservation-slot-sec", type=int, default=1)
    parser.add_argument("--reservation-delay-step-sec", type=float, default=2.0)
    parser.add_argument("--reservation-max-delay-sec", type=float, default=180.0)
    parser.add_argument("--reservation-reserve-wait-nodes", action="store_true")
    parser.add_argument("--reservation-retention-sec", type=float, default=30.0)
    parser.add_argument("--reservation-deadlock-backoff-sec", type=float, default=5.0)
    parser.add_argument("--max-route-candidates", type=int, default=8)
    parser.add_argument("--max-route-candidates-real", type=int, default=8)
    parser.add_argument("--max-route-candidates-reserved", type=int, default=8)
    parser.add_argument("--max-route-candidates-redundant", type=int, default=4)
    parser.add_argument("--max-active-real-planning-subtasks", type=int, default=0)
    parser.add_argument("--max-active-redundant-planning-subtasks", type=int, default=0)
    parser.add_argument("--max-active-real-planning-per-task", type=int, default=0)
    parser.add_argument("--max-active-redundant-planning-per-task", type=int, default=0)
    parser.add_argument("--max-active-real-task-groups", type=int, default=0)
    parser.add_argument("--max-active-redundant-task-groups", type=int, default=0)
    parser.add_argument("--vehicle-platform-score-enabled", default="true")
    parser.add_argument("--vehicle-platform-score-request-limit", type=int, default=24)
    parser.add_argument("--project-book-enabled", default="true")
    parser.add_argument("--depot-book-enabled", default="true")
    parser.add_argument("--vehicle-realtime-scale", type=float, default=0.08)
    parser.add_argument("--vehicle-speed-mps", type=float, default=22.222)
    parser.add_argument("--simulation-mode", default="realtime")
    parser.add_argument("--depot-capacity", type=int, default=16)
    parser.add_argument("--reload-duration-sec", type=float, default=60.0)
    parser.add_argument("--redundancy-enabled", default="false")
    parser.add_argument("--redundancy-ratio", type=float, default=0.2)
    parser.add_argument("--hide-trigger-slack-sec", type=float, default=60.0)
    parser.add_argument("--hide-min-wait-sec", type=float, default=30.0)
    parser.add_argument("--hot-distance-threshold-m", type=float, default=5000.0)
    parser.add_argument("--cold-distance-threshold-m", type=float, default=5000.0)
    parser.add_argument("--launch-prepare-sec", type=float, default=300.0)
    parser.add_argument("--hot-standby-sec", type=float, default=180.0)
    parser.add_argument("--cold-standby-sec", type=float, default=420.0)
    parser.add_argument("--hot-startup-sec", type=float, default=180.0)
    parser.add_argument("--cold-startup-sec", type=float, default=420.0)
    args = parser.parse_args()

    bbox = parse_bbox(args.bbox)
    classes = {x.strip() for x in args.classes.split(",") if x.strip()}
    out_root = Path(args.out_root)
    data_dir = out_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    graph_json = data_dir / "road_graph_shp_demo.json"
    points_json = data_dir / "special_points_shp_demo.json"
    meta_path = out_root / "build_meta.json"
    roads_shp = Path(args.roads_shp)

    hide_strategy_cfg = {
        "trigger_slack_sec": args.hide_trigger_slack_sec,
        "min_wait_sec": args.hide_min_wait_sec,
    }
    launch_timing_cfg = {
        "hot_distance_threshold_m": args.hot_distance_threshold_m,
        "cold_distance_threshold_m": args.cold_distance_threshold_m,
        "launch_prepare_sec": args.launch_prepare_sec,
        "hot_standby_sec": args.hot_standby_sec,
        "cold_standby_sec": args.cold_standby_sec,
        "hot_startup_sec": args.hot_standby_sec,
        "cold_startup_sec": args.cold_standby_sec,
    }
    redundancy_cfg = {
        "enabled": boolish(args.redundancy_enabled),
        "ratio": max(0.0, float(args.redundancy_ratio)),
    }
    cache_inputs = {
        "roads_shp": file_signature(roads_shp),
        "bbox_lonlat": [round(v, 8) for v in bbox],
        "classes": sorted(classes),
        "node_snap_tol_m": round(float(args.node_snap_tol_m), 3),
        "fire_zone_enabled": boolish(args.fire_zone_enabled),
        "fire_zone_axis": args.fire_zone_axis,
        "fire_zone_side": args.fire_zone_side,
        "fire_zone_bonus": float(args.fire_zone_bonus),
        "vehicles": int(args.vehicles),
        "vehicle_ports": [int(p) for p in str(args.vehicle_ports or "").split(",") if p.strip()],
        "hide_point_count": int(args.hide_point_count),
        "launch_point_count": int(args.launch_point_count),
        "hide_strategy": hide_strategy_cfg,
        "launch_timing": launch_timing_cfg,
        "redundancy": redundancy_cfg,
    }
    cache_key = build_cache_key(cache_inputs)

    graph = None
    points = None
    homes = None
    fire_zone_cfg = None
    meta = {}
    cache_hit = False
    if meta_path.exists() and graph_json.exists() and points_json.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cache_hit = meta.get("cache_key") == cache_key
        except Exception:
            cache_hit = False
    if cache_hit:
        homes = list(meta.get("vehicle_homes", []))
        fire_zone_cfg = dict(meta.get("fire_zone", {}))
        try:
            points = json.loads(points_json.read_text(encoding="utf-8"))
        except Exception:
            cache_hit = False
        if not homes or not fire_zone_cfg or not points:
            cache_hit = False

    if not cache_hit:
        graph = build_graph_from_shp(
            roads_shp=roads_shp,
            bbox_lonlat=bbox,
            include_classes=classes,
            node_snap_tol_m=args.node_snap_tol_m,
        )
        keep = largest_component(graph)
        graph = prune_to_component(graph, keep)
        graph = compact_linear_graph(graph)
        fire_zone_cfg = build_fire_zone_cfg(
            graph=graph,
            enabled=boolish(args.fire_zone_enabled),
            axis=args.fire_zone_axis,
            side=args.fire_zone_side,
            bonus_score=args.fire_zone_bonus,
        )
        points = pick_demo_points(
            graph,
            hide_point_count=args.hide_point_count,
            launch_point_count=args.launch_point_count,
        )
        explicit_vehicle_ports = [int(p) for p in str(args.vehicle_ports or "").split(",") if p.strip()]
        vehicle_count_for_homes = len(explicit_vehicle_ports) if explicit_vehicle_ports else int(args.vehicles)
        homes = choose_vehicle_homes(graph, count=max(4, vehicle_count_for_homes))
        save_graph_json(graph, graph_json)
        points_json.write_text(json.dumps(points, ensure_ascii=True, indent=2), encoding="utf-8")
    else:
        graph_nodes = int(meta.get("graph_nodes", 0))
        graph_edges = int(meta.get("graph_edges", 0))

    build_scheduler_and_vehicle_configs(
        out_root=out_root,
        graph_json=graph_json,
        points_json=points_json,
        vehicle_homes=homes,
        vehicles=args.vehicles,
        scheduler_port=args.scheduler_port,
        dashboard_port=args.dashboard_port,
        vehicle_port_base=args.vehicle_port_base,
        vehicle_ports=[int(p) for p in str(args.vehicle_ports or "").split(",") if p.strip()],
        fire_zone_cfg=fire_zone_cfg,
        scheduler_tick_sec=args.scheduler_tick_sec,
        heartbeat_timeout_sec=args.heartbeat_timeout_sec,
        proposal_timeout_sec=args.proposal_timeout_sec,
        max_proposal_retries=args.max_proposal_retries,
        reservation_slot_sec=args.reservation_slot_sec,
        reservation_delay_step_sec=args.reservation_delay_step_sec,
        reservation_max_delay_sec=args.reservation_max_delay_sec,
        reservation_reserve_wait_nodes=args.reservation_reserve_wait_nodes,
        reservation_retention_sec=args.reservation_retention_sec,
        reservation_deadlock_backoff_sec=args.reservation_deadlock_backoff_sec,
        vehicle_realtime_scale=args.vehicle_realtime_scale,
        vehicle_speed_mps=args.vehicle_speed_mps,
        simulation_mode=str(args.simulation_mode).lower(),
        depot_capacity=args.depot_capacity,
        reload_duration_sec=args.reload_duration_sec,
        hide_strategy_cfg=hide_strategy_cfg,
        launch_timing_cfg=launch_timing_cfg,
        redundancy_cfg=redundancy_cfg,
        max_route_candidates=args.max_route_candidates,
        max_route_candidates_real=args.max_route_candidates_real,
        max_route_candidates_reserved=args.max_route_candidates_reserved,
        max_route_candidates_redundant=args.max_route_candidates_redundant,
        max_active_real_planning_subtasks=args.max_active_real_planning_subtasks,
        max_active_redundant_planning_subtasks=args.max_active_redundant_planning_subtasks,
        max_active_real_planning_per_task=args.max_active_real_planning_per_task,
        max_active_redundant_planning_per_task=args.max_active_redundant_planning_per_task,
        max_active_real_task_groups=args.max_active_real_task_groups,
        max_active_redundant_task_groups=args.max_active_redundant_task_groups,
        vehicle_platform_score_enabled=boolish(args.vehicle_platform_score_enabled),
        vehicle_platform_score_request_limit=args.vehicle_platform_score_request_limit,
        project_book_enabled=boolish(args.project_book_enabled),
        depot_book_enabled=boolish(args.depot_book_enabled),
    )

    if graph is not None:
        graph_nodes = len(graph.nodes)
        graph_edges = len(graph.edge_meta)
    meta = {
        "cache_key": cache_key,
        "cache_inputs": cache_inputs,
        "roads_shp": str(roads_shp),
        "bbox_lonlat": bbox,
        "classes": sorted(classes),
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "vehicle_homes": homes,
        "fire_zone": fire_zone_cfg,
        "hide_strategy": hide_strategy_cfg,
        "launch_timing": launch_timing_cfg,
        "redundancy": redundancy_cfg,
        "generated_at": utc_now_iso(),
        "cache_hit": cache_hit,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")

    print(f"Generated SHP demo root under: {out_root}")
    print(f"Build cache: {'hit' if cache_hit else 'miss'}")
    print(f"Graph: {graph_json}")
    print(f"Points: {points_json}")
    print(f"Scheduler config: {out_root / 'configs' / 'scheduler_debug.json'}")
    print(f"Vehicles dir: {out_root / 'configs' / 'vehicles'}")
    print(f"Tasks: {out_root / 'tasks' / 'task_shp_beijing_001.json'}, {out_root / 'tasks' / 'task_shp_beijing_002.json'}")
    print(f"Graph size: nodes={graph_nodes} edges={graph_edges}")


if __name__ == "__main__":
    main()
