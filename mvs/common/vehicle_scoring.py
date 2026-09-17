from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Sequence

from mvs.common.scoring import vehicle_score as _base_vehicle_score
from mvs.scheduler.map_model import RoadGraph
from mvs.common.lane_graph import LaneGraph


def point_in_bounds_xy(x: float, y: float, bounds: Sequence[float]) -> bool:
    if len(bounds) < 4:
        return False
    min_x, min_y, max_x, max_y = [float(v) for v in bounds[:4]]
    return min(min_x, max_x) <= float(x) <= max(min_x, max_x) and min(min_y, max_y) <= float(y) <= max(min_y, max_y)


def node_in_bounds(node: Any, bounds: Sequence[float]) -> bool:
    """Match configured fire-pool bounds; prefer lon/lat, then graph x/y."""
    if not bounds:
        return False
    lon = getattr(node, "lon", None)
    lat = getattr(node, "lat", None)
    if lon is not None and lat is not None and point_in_bounds_xy(float(lon), float(lat), bounds):
        return True
    return point_in_bounds_xy(float(getattr(node, "x", 0.0)), float(getattr(node, "y", 0.0)), bounds)


def task_book_vehicle_score(
    *,
    vehicle_id: str,
    current_node: str,
    launch_node: Optional[str],
    ammo_types: Iterable[str],
    required_ammo_type: Optional[str],
    speed_mps: float,
    health: float,
    graph: Optional[RoadGraph],
    lane_graph: Optional[LaneGraph] = None,
    request_time: Optional[str] = None,
    desired_fire_time: Optional[str] = None,
    fire_time_grace_sec: float = 15.0,
    ignore_speed_limits: bool = False,
    fire_pool_bounds_xy: Sequence[float] = (),
    fire_pool_bonus: float = 0.0,
    health_weight: float = 0.15,
) -> Dict[str, Any]:
    """Task-book vehicle score: base feasibility + health + fire-pool bonus."""
    row = _base_vehicle_score(
        vehicle_id=vehicle_id,
        current_node=current_node,
        launch_node=launch_node,
        ammo_types=ammo_types,
        required_ammo_type=required_ammo_type,
        speed_mps=speed_mps,
        health=health,
        graph=graph,
        lane_graph=lane_graph,
        request_time=request_time,
        desired_fire_time=desired_fire_time,
        fire_time_grace_sec=fire_time_grace_sec,
        ignore_speed_limits=ignore_speed_limits,
    )
    in_fire_pool = False
    if graph and current_node in graph.nodes and fire_pool_bounds_xy:
        node = graph.nodes[current_node]
        in_fire_pool = node_in_bounds(node, fire_pool_bounds_xy)
    health_score = max(0.0, min(100.0, float(health) * 100.0))
    health_delta = max(0.0, float(health_weight)) * (health_score - float(row.get("score_health", health_score) or 0.0))
    bonus = float(fire_pool_bonus) if in_fire_pool else 0.0
    total = max(0.0, min(100.0, float(row.get("score_total", 0.0) or 0.0) + health_delta + bonus))
    row.update(
        {
            "vehicle_health": round(float(health), 6),
            "score_health": round(health_score, 3),
            "fire_pool_in_zone": bool(in_fire_pool),
            "score_fire_pool_bonus": round(bonus, 3),
            "score_total_before_fire_pool": row.get("score_total"),
            "score_total": round(total, 3),
            "score_semantics": "task_book_vehicle_health_fire_pool",
        }
    )
    return row
