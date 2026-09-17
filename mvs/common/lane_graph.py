from __future__ import annotations

import heapq
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from mvs.common.formula_utils import topology_total_cost
from mvs.scheduler.map_model import EdgeMeta, RoadGraph


@dataclass
class LaneSegment:
    lane_id: str
    src_node: str
    dst_node: str
    edge_id: str
    direction: int
    lane_width_m: float
    speed_limit_mps: float
    length_m: float
    curvature: float
    polyline: List[Tuple[float, float]]


@dataclass
class LaneRoute:
    lane_ids: List[str]
    road_node_path: List[str]
    distance_m: float
    travel_sec: float
    centerline: List[Tuple[float, float]]


class LaneGraph:
    def __init__(self, road_graph: RoadGraph) -> None:
        self.road_graph = road_graph
        self.lanes: Dict[str, LaneSegment] = {}
        self.adj: Dict[str, List[Tuple[str, float]]] = {}
        self.outgoing_by_node: Dict[str, List[str]] = {}
        self.incoming_by_node: Dict[str, List[str]] = {}
        self._route_cache: OrderedDict[Tuple[str, str, int, bool], Tuple[List[str], LaneRoute]] = OrderedDict()
        self._route_cache_limit = 4096
        self.route_queries = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_evictions = 0
        self._build()

    def set_cache_limit(self, limit: int) -> None:
        self._route_cache_limit = max(128, int(limit))
        while len(self._route_cache) > self._route_cache_limit:
            self._route_cache.popitem(last=False)
            self.cache_evictions += 1

    def prewarm_routes(
        self,
        start_nodes: Sequence[str],
        goal_nodes: Sequence[str],
        speed_caps_mps: Optional[Sequence[Optional[float]]] = None,
    ) -> dict:
        unique_starts = [str(x) for x in dict.fromkeys(start_nodes) if str(x)]
        unique_goals = [str(x) for x in dict.fromkeys(goal_nodes) if str(x)]
        speed_caps = list(speed_caps_mps or [None])
        if not speed_caps:
            speed_caps = [None]
        seen_caps = []
        for cap in speed_caps:
            if cap not in seen_caps:
                seen_caps.append(cap)
        before_queries = self.route_queries
        before_hits = self.cache_hits
        before_misses = self.cache_misses
        attempted = 0
        for speed_cap in seen_caps:
            for start_node in unique_starts:
                for goal_node in unique_goals:
                    if start_node == goal_node:
                        continue
                    attempted += 1
                    self.route_between_nodes(start_node, goal_node, speed_cap_mps=speed_cap)
        return {
            "start_nodes": len(unique_starts),
            "goal_nodes": len(unique_goals),
            "speed_caps": len(seen_caps),
            "attempted_pairs": attempted,
            "route_queries_added": self.route_queries - before_queries,
            "cache_hits_added": self.cache_hits - before_hits,
            "cache_misses_added": self.cache_misses - before_misses,
        }

    def route_between_nodes(
        self,
        start_node: str,
        goal_node: str,
        speed_cap_mps: Optional[float] = None,
        ignore_speed_limits: bool = False,
    ) -> Tuple[List[str], LaneRoute]:
        # 任务书“拓扑节点地图节点级路径搜索”：在车道图上执行 Dijkstra。
        # 单车阶段不扣减 cap=2，只把车道/路段属性保留下来，容量冲突交给调度预约表处理。
        self.route_queries += 1
        if start_node == goal_node:
            return [], LaneRoute(lane_ids=[], road_node_path=[start_node], distance_m=0.0, travel_sec=0.0, centerline=[])

        cache_key = (
            start_node,
            goal_node,
            int(round((speed_cap_mps or 0.0) * 10.0)),
            bool(ignore_speed_limits),
        )
        if cache_key in self._route_cache:
            self.cache_hits += 1
            lane_ids, route = self._route_cache.pop(cache_key)
            self._route_cache[cache_key] = (lane_ids, route)
            return self._clone_route(lane_ids, route)
        self.cache_misses += 1

        start_lanes = list(self.outgoing_by_node.get(start_node, []))
        goal_lanes = set(self.incoming_by_node.get(goal_node, []))
        if not start_lanes or not goal_lanes:
            return [], LaneRoute(lane_ids=[], road_node_path=[], distance_m=float("inf"), travel_sec=float("inf"), centerline=[])

        pq: List[Tuple[float, str]] = []
        dist: Dict[str, float] = {}
        prev: Dict[str, Optional[str]] = {}
        lane_distance: Dict[str, float] = {}
        lane_travel_time: Dict[str, float] = {}

        for lane_id in start_lanes:
            lane = self.lanes[lane_id]
            lane_time = self._lane_time(lane, speed_cap_mps, ignore_speed_limits)
            dist[lane_id] = lane_time
            lane_distance[lane_id] = lane.length_m
            lane_travel_time[lane_id] = lane_time
            prev[lane_id] = None
            heapq.heappush(pq, (lane_time, lane_id))

        goal_lane: Optional[str] = None
        while pq:
            cur_cost, lane_id = heapq.heappop(pq)
            if cur_cost > dist.get(lane_id, float("inf")):
                continue
            if lane_id in goal_lanes:
                goal_lane = lane_id
                break
            for next_lane, turn_penalty in self.adj.get(lane_id, []):
                # 公式(12) topology_total_cost 实际使用点：Dijkstra 每扩展一条车道边时，
                # 把行驶时间和转向曲率惩罚合成统一代价，决定最终选哪条路。
                step_cost = topology_total_cost(
                    climb_cost=0.0,
                    distance_cost=self._lane_time(self.lanes[next_lane], speed_cap_mps, ignore_speed_limits),
                    landmark_cost=0.0,
                    curvature_cost=turn_penalty,
                )
                nd = cur_cost + step_cost
                if nd < dist.get(next_lane, float("inf")):
                    dist[next_lane] = nd
                    lane_distance[next_lane] = lane_distance[lane_id] + self.lanes[next_lane].length_m
                    lane_travel_time[next_lane] = (
                        lane_travel_time[lane_id]
                        + self._lane_time(self.lanes[next_lane], speed_cap_mps, ignore_speed_limits)
                    )
                    prev[next_lane] = lane_id
                    heapq.heappush(pq, (nd, next_lane))

        if goal_lane is None:
            return [], LaneRoute(lane_ids=[], road_node_path=[], distance_m=float("inf"), travel_sec=float("inf"), centerline=[])

        lane_ids = self._reconstruct(goal_lane, prev)
        road_nodes = [start_node]
        for lane_id in lane_ids:
            road_nodes.append(self.lanes[lane_id].dst_node)
        route = LaneRoute(
            lane_ids=lane_ids,
            road_node_path=road_nodes,
            distance_m=lane_distance[goal_lane],
            travel_sec=lane_travel_time[goal_lane],
            centerline=self._route_centerline(lane_ids),
        )
        self._cache_route(cache_key, lane_ids, route)
        return self._clone_route(lane_ids, route)

    def _cache_route(self, cache_key: Tuple[str, str, int, bool], lane_ids: List[str], route: LaneRoute) -> None:
        self._route_cache[cache_key] = (list(lane_ids), route)
        if len(self._route_cache) > self._route_cache_limit:
            self._route_cache.popitem(last=False)
            self.cache_evictions += 1

    @staticmethod
    def _clone_route(lane_ids: List[str], route: LaneRoute) -> Tuple[List[str], LaneRoute]:
        return list(lane_ids), LaneRoute(
            lane_ids=list(route.lane_ids),
            road_node_path=list(route.road_node_path),
            distance_m=route.distance_m,
            travel_sec=route.travel_sec,
            centerline=list(route.centerline),
        )

    def stats(self) -> dict:
        total = self.cache_hits + self.cache_misses
        hit_rate = (self.cache_hits / total) if total > 0 else 0.0
        return {
            "route_queries": self.route_queries,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(hit_rate, 4),
            "cache_size": len(self._route_cache),
            "cache_limit": self._route_cache_limit,
            "cache_evictions": self.cache_evictions,
            "lane_count": len(self.lanes),
        }

    def node_lane_pose(self, node_id: str, lane_id: str, use_start: bool) -> Optional[Tuple[float, float, float, float]]:
        lane = self.lanes.get(lane_id)
        if lane is None or not lane.polyline:
            return None
        pts = lane.polyline
        if use_start:
            idx1, idx2 = 0, min(1, len(pts) - 1)
        else:
            idx1, idx2 = max(0, len(pts) - 2), len(pts) - 1
        x1, y1 = pts[idx1]
        x2, y2 = pts[idx2]
        yaw = math.atan2(y2 - y1, x2 - x1)
        if not use_start:
            x1, y1 = pts[-1]
        return x1, y1, yaw, lane.lane_width_m

    def roadside_pose(self, node_id: str, lane_id: str, use_start: bool, offset_m: float = 2.0) -> Optional[Tuple[float, float, float]]:
        pose = self.node_lane_pose(node_id, lane_id, use_start)
        if pose is None:
            return None
        x, y, yaw, lane_width = pose
        nx = -math.sin(yaw)
        ny = math.cos(yaw)
        side_offset = lane_width / 2.0 + offset_m
        return x + nx * side_offset, y + ny * side_offset, yaw

    def _build(self) -> None:
        for meta in self.road_graph.edge_meta.values():
            lane_width = max(0.5, meta.lane_width)
            base = self._oriented_geometry(meta, meta.src, meta.dst)
            if meta.lanes_forward > 0:
                lane_id = f"{meta.edge_id}:f"
                poly = list(base)
                self.lanes[lane_id] = LaneSegment(
                    lane_id=lane_id,
                    src_node=meta.src,
                    dst_node=meta.dst,
                    edge_id=meta.edge_id,
                    direction=1,
                    lane_width_m=lane_width,
                    speed_limit_mps=meta.speed_limit_mps,
                    length_m=self._polyline_length(poly),
                    curvature=max(0.0, float(meta.curvature)),
                    polyline=poly,
                )
            if meta.lanes_backward > 0:
                lane_id = f"{meta.edge_id}:b"
                reversed_base = list(reversed(base))
                poly = reversed_base
                self.lanes[lane_id] = LaneSegment(
                    lane_id=lane_id,
                    src_node=meta.dst,
                    dst_node=meta.src,
                    edge_id=meta.edge_id,
                    direction=-1,
                    lane_width_m=lane_width,
                    speed_limit_mps=meta.speed_limit_mps,
                    length_m=self._polyline_length(poly),
                    curvature=max(0.0, float(meta.curvature)),
                    polyline=poly,
                )

        for lane_id, lane in self.lanes.items():
            self.outgoing_by_node.setdefault(lane.src_node, []).append(lane_id)
            self.incoming_by_node.setdefault(lane.dst_node, []).append(lane_id)

        for lane_id, lane in self.lanes.items():
            next_lanes = self.outgoing_by_node.get(lane.dst_node, [])
            self.adj[lane_id] = []
            end_heading = self._heading_from_polyline(lane.polyline, at_end=True)
            for next_lane_id in next_lanes:
                next_lane = self.lanes[next_lane_id]
                if next_lane.dst_node == lane.src_node:
                    # Avoid immediate U-turn / backtracking on the just-traversed segment.
                    continue
                start_heading = self._heading_from_polyline(next_lane.polyline, at_end=False)
                # 公式(12) topology_total_cost 实际使用点：预先计算相邻车道的转向惩罚。
                # 后续 Dijkstra 扩展时把它作为 curvature_cost，避免明显急转/折返路线。
                turn_penalty = topology_total_cost(
                    climb_cost=0.0,
                    distance_cost=0.0,
                    landmark_cost=0.0,
                    curvature_cost=abs(self._angle_diff(end_heading, start_heading)) / math.pi,
                )
                self.adj[lane_id].append((next_lane_id, turn_penalty))

    def _lane_time(
        self,
        lane: LaneSegment,
        speed_cap_mps: Optional[float],
        ignore_speed_limits: bool = False,
    ) -> float:
        if ignore_speed_limits and speed_cap_mps is not None:
            return lane.length_m / max(0.5, speed_cap_mps)
        speed = max(0.5, lane.speed_limit_mps)
        if speed_cap_mps is not None:
            speed = min(speed, max(0.5, speed_cap_mps))
        return lane.length_m / speed

    def _reconstruct(self, goal_lane: str, prev: Dict[str, Optional[str]]) -> List[str]:
        out: List[str] = []
        cur: Optional[str] = goal_lane
        while cur is not None:
            out.append(cur)
            cur = prev.get(cur)
        out.reverse()
        return out

    def _route_centerline(self, lane_ids: Sequence[str]) -> List[Tuple[float, float]]:
        out: List[Tuple[float, float]] = []
        for lane_id in lane_ids:
            lane = self.lanes[lane_id]
            for x, y in lane.polyline:
                if not out or math.hypot(x - out[-1][0], y - out[-1][1]) > 1e-3:
                    if len(out) >= 2 and math.hypot(x - out[-2][0], y - out[-2][1]) <= 1e-3:
                        # Collapse local spike A->B->A introduced by inconsistent segment orientation.
                        out.pop()
                        continue
                    out.append((x, y))
        return self._simplify_collinear(out)

    def _oriented_geometry(self, meta: EdgeMeta, src: str, dst: str) -> List[Tuple[float, float]]:
        if meta.geometry:
            pts = [(float(p["x"]), float(p["y"])) for p in meta.geometry]
        else:
            a = self.road_graph.nodes[src]
            b = self.road_graph.nodes[dst]
            pts = [(a.x, a.y), (b.x, b.y)]
        return pts if meta.geom_from == src and meta.geom_to == dst else list(reversed(pts))

    @staticmethod
    def _simplify_collinear(polyline: Sequence[Tuple[float, float]], angle_tol_deg: float = 4.0) -> List[Tuple[float, float]]:
        if len(polyline) <= 2:
            return list(polyline)
        out: List[Tuple[float, float]] = [polyline[0]]
        for idx in range(1, len(polyline) - 1):
            ax, ay = out[-1]
            bx, by = polyline[idx]
            cx, cy = polyline[idx + 1]
            abx, aby = bx - ax, by - ay
            bcx, bcy = cx - bx, cy - by
            nab = math.hypot(abx, aby)
            nbc = math.hypot(bcx, bcy)
            if nab <= 1e-6 or nbc <= 1e-6:
                continue
            dot = max(-1.0, min(1.0, (abx * bcx + aby * bcy) / (nab * nbc)))
            turn = math.degrees(math.acos(dot))
            if turn <= angle_tol_deg:
                continue
            out.append((bx, by))
        out.append(polyline[-1])
        return out

    @staticmethod
    def _offset_polyline(polyline: Sequence[Tuple[float, float]], offset_m: float) -> List[Tuple[float, float]]:
        if len(polyline) < 2:
            return list(polyline)
        out: List[Tuple[float, float]] = []
        for idx, (x, y) in enumerate(polyline):
            if idx == 0:
                dx = polyline[1][0] - x
                dy = polyline[1][1] - y
            elif idx == len(polyline) - 1:
                dx = x - polyline[idx - 1][0]
                dy = y - polyline[idx - 1][1]
            else:
                dx = polyline[idx + 1][0] - polyline[idx - 1][0]
                dy = polyline[idx + 1][1] - polyline[idx - 1][1]
            norm = math.hypot(dx, dy)
            if norm <= 1e-6:
                out.append((x, y))
                continue
            nx = -dy / norm
            ny = dx / norm
            out.append((x + nx * offset_m, y + ny * offset_m))
        return out

    @staticmethod
    def _polyline_length(polyline: Sequence[Tuple[float, float]]) -> float:
        total = 0.0
        for i in range(len(polyline) - 1):
            total += math.hypot(polyline[i + 1][0] - polyline[i][0], polyline[i + 1][1] - polyline[i][1])
        return total

    @staticmethod
    def _heading_from_polyline(polyline: Sequence[Tuple[float, float]], at_end: bool) -> float:
        if len(polyline) < 2:
            return 0.0
        if at_end:
            a = polyline[-2]
            b = polyline[-1]
        else:
            a = polyline[0]
            b = polyline[1]
        return math.atan2(b[1] - a[1], b[0] - a[0])

    @staticmethod
    def _angle_diff(a: float, b: float) -> float:
        return (b - a + math.pi) % (2.0 * math.pi) - math.pi
