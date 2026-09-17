from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from mvs.common.formula_utils import topology_total_cost
from mvs.scheduler.map_model import EdgeMeta, RoadGraph


@dataclass
class VehicleKinematics:
    width_m: float = 2.8
    length_m: float = 7.5
    wheelbase_m: float = 4.2
    min_turn_radius_m: float = 12.0
    max_steer_deg: float = 28.0
    max_turn_angle_deg: float = 105.0
    min_clearance_m: float = 0.8
    turn_penalty: float = 10.0
    max_edge_curvature: Optional[float] = None
    max_speed_mps: Optional[float] = None

    @property
    def min_lane_width_m(self) -> float:
        return self.width_m + self.min_clearance_m


@dataclass
class PathConstraintReport:
    feasible: bool
    algorithm: str = "constrained_a_star"
    violations: List[str] = field(default_factory=list)
    path_distance_m: float = 0.0
    estimated_travel_seconds: float = 0.0
    narrowest_width_m: Optional[float] = None
    max_turn_angle_deg: float = 0.0
    min_turn_radius_m: Optional[float] = None
    total_turn_penalty: float = 0.0

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "algorithm": self.algorithm,
            "violations": list(self.violations),
            "path_distance_m": round(self.path_distance_m, 3),
            "estimated_travel_seconds": round(self.estimated_travel_seconds, 3),
            "narrowest_width_m": None if self.narrowest_width_m is None else round(self.narrowest_width_m, 3),
            "max_turn_angle_deg": round(self.max_turn_angle_deg, 3),
            "min_turn_radius_m": None if self.min_turn_radius_m is None else round(self.min_turn_radius_m, 3),
            "total_turn_penalty": round(self.total_turn_penalty, 3),
        }


class KinematicPathPlanner:
    def __init__(self, graph: RoadGraph, limits: VehicleKinematics) -> None:
        self.graph = graph
        self.limits = limits

    def plan(self, start: str, goal: str) -> Tuple[List[str], PathConstraintReport]:
        # 任务书“多车型运动学差异”：拓扑搜索时过滤过窄道路、过大曲率和转弯半径违规。
        # 该结果作为 Hybrid A* 的走廊，既保留快速搜索，也避免明显不可执行的路径。
        if start not in self.graph.nodes or goal not in self.graph.nodes:
            return [], PathConstraintReport(feasible=False, violations=["start_or_goal_missing"])
        if start == goal:
            return [start], PathConstraintReport(feasible=True)

        pq: List[Tuple[float, float, Tuple[Optional[str], str]]] = [(self._heuristic_seconds(start, goal), 0.0, (None, start))]
        dist: Dict[Tuple[Optional[str], str], float] = {(None, start): 0.0}
        prev: Dict[Tuple[Optional[str], str], Optional[Tuple[Optional[str], str]]] = {(None, start): None}

        best_goal_state: Optional[Tuple[Optional[str], str]] = None
        while pq:
            _, cost_so_far, state = heapq.heappop(pq)
            if cost_so_far > dist.get(state, float("inf")):
                continue
            prev_node, cur = state
            if cur == goal:
                best_goal_state = state
                break
            for nxt, edge_cost in self.graph.edges.get(cur, []):
                ok, turn_penalty, _ = self._transition_metrics(prev_node, cur, nxt)
                if not ok:
                    continue
                meta = self.graph.get_edge_meta(cur, nxt)
                if meta is None:
                    continue
                # 公式(12) topology_total_cost 实际使用点：车辆运动学约束搜索的边代价。
                # 这里把路段时间和转弯惩罚合并，过滤不满足转弯/宽度约束后的可行走廊。
                nd = cost_so_far + topology_total_cost(
                    climb_cost=0.0,
                    distance_cost=self._edge_time_cost(meta),
                    landmark_cost=0.0,
                    curvature_cost=turn_penalty,
                )
                next_state = (cur, nxt)
                if nd < dist.get(next_state, float("inf")):
                    dist[next_state] = nd
                    prev[next_state] = state
                    priority = nd + self._heuristic_seconds(nxt, goal)
                    heapq.heappush(pq, (priority, nd, next_state))

        if best_goal_state is None:
            return [], PathConstraintReport(feasible=False, violations=["no_kinematic_path"])

        path = self._reconstruct(best_goal_state, prev)
        return path, self.analyze_path(path)

    def analyze_path(self, node_path: List[str]) -> PathConstraintReport:
        if not node_path:
            return PathConstraintReport(feasible=False, violations=["empty_path"])
        if len(node_path) == 1:
            return PathConstraintReport(feasible=True)

        report = PathConstraintReport(feasible=True, narrowest_width_m=float("inf"), min_turn_radius_m=float("inf"))
        for i in range(len(node_path) - 1):
            a, b = node_path[i], node_path[i + 1]
            meta = self.graph.get_edge_meta(a, b)
            if meta is None:
                report.feasible = False
                report.violations.append(f"edge_missing:{a}->{b}")
                continue
            report.path_distance_m += meta.cost
            report.estimated_travel_seconds += self._edge_time_cost(meta)
            report.narrowest_width_m = min(report.narrowest_width_m or meta.width, meta.width)
            if meta.lane_width < self.limits.min_lane_width_m:
                report.feasible = False
                report.violations.append(f"lane_width:{a}->{b}:{meta.lane_width:.2f}")
            if self.limits.max_edge_curvature is not None and meta.curvature > self.limits.max_edge_curvature:
                report.feasible = False
                report.violations.append(f"curvature:{a}->{b}:{meta.curvature:.4f}")

        for i in range(1, len(node_path) - 1):
            prev_node = node_path[i - 1]
            cur = node_path[i]
            nxt = node_path[i + 1]
            ok, turn_penalty, info = self._transition_metrics(prev_node, cur, nxt)
            angle = info.get("turn_angle_deg", 0.0)
            radius = info.get("turn_radius_m")
            report.max_turn_angle_deg = max(report.max_turn_angle_deg, angle)
            report.total_turn_penalty += turn_penalty
            if radius is not None:
                report.min_turn_radius_m = min(report.min_turn_radius_m or radius, radius)
            if not ok:
                report.feasible = False
                report.violations.extend(info.get("violations", []))

        if report.narrowest_width_m == float("inf"):
            report.narrowest_width_m = None
        if report.min_turn_radius_m == float("inf"):
            report.min_turn_radius_m = None
        return report

    def _reconstruct(
        self,
        goal_state: Tuple[Optional[str], str],
        prev: Dict[Tuple[Optional[str], str], Optional[Tuple[Optional[str], str]]],
    ) -> List[str]:
        states: List[Tuple[Optional[str], str]] = []
        cur: Optional[Tuple[Optional[str], str]] = goal_state
        while cur is not None:
            states.append(cur)
            cur = prev.get(cur)
        states.reverse()

        path: List[str] = []
        for _, node_id in states:
            if not path or path[-1] != node_id:
                path.append(node_id)
        return path

    def _transition_metrics(
        self,
        prev_node: Optional[str],
        cur_node: str,
        next_node: str,
    ) -> Tuple[bool, float, dict]:
        meta = self.graph.get_edge_meta(cur_node, next_node)
        if meta is None:
            return False, 0.0, {"violations": [f"edge_missing:{cur_node}->{next_node}"]}
        if meta.lane_width < self.limits.min_lane_width_m:
            return False, 0.0, {"violations": [f"lane_width:{cur_node}->{next_node}:{meta.lane_width:.2f}"]}
        if self.limits.max_edge_curvature is not None and meta.curvature > self.limits.max_edge_curvature:
            return False, 0.0, {"violations": [f"curvature:{cur_node}->{next_node}:{meta.curvature:.4f}"]}
        if prev_node is None:
            return True, 0.0, {"turn_angle_deg": 0.0}

        incoming_heading = self._edge_heading(prev_node, cur_node, forward=True)
        outgoing_heading = self._edge_heading(cur_node, next_node, forward=True)
        angle = self._angle_diff_deg(incoming_heading, outgoing_heading)
        if angle > self.limits.max_turn_angle_deg:
            return False, 0.0, {"turn_angle_deg": angle, "violations": [f"turn_angle:{cur_node}:{angle:.1f}"]}

        in_cost = self.graph.get_edge_cost(prev_node, cur_node) or 0.0
        out_cost = self.graph.get_edge_cost(cur_node, next_node) or 0.0
        radius = self._approx_turn_radius(in_cost, out_cost, angle)
        if radius is not None and radius < self.limits.min_turn_radius_m:
            return False, 0.0, {"turn_angle_deg": angle, "turn_radius_m": radius, "violations": [f"turn_radius:{cur_node}:{radius:.2f}"]}

        penalty = 0.0
        if angle > 12.0:
            penalty = (angle / 90.0) * self.limits.turn_penalty
        return True, penalty, {"turn_angle_deg": angle, "turn_radius_m": radius}

    def _heuristic_seconds(self, node_id: str, goal_id: str) -> float:
        a = self.graph.nodes[node_id]
        b = self.graph.nodes[goal_id]
        dist = math.hypot(a.x - b.x, a.y - b.y)
        speed = max(0.5, self.limits.max_speed_mps or 8.0)
        return dist / speed

    def _edge_time_cost(self, meta: EdgeMeta) -> float:
        # The fallback planner follows the same mission-speed convention as
        # the lane planner: road-class defaults do not cap vehicle speed.
        speed = self.limits.max_speed_mps if self.limits.max_speed_mps is not None else meta.speed_limit_mps
        speed = max(0.5, speed)
        curvature_penalty = 1.0 + min(0.6, max(0.0, meta.curvature) * max(1.0, self.limits.min_turn_radius_m) * 0.35)
        base_time = meta.cost / speed
        # 公式(12) topology_total_cost 实际使用点：把基础行驶时间和曲率放大项合成边时间。
        # 该值进入上面的 Dijkstra cost_so_far，直接影响候选路径排序。
        return topology_total_cost(
            climb_cost=0.0,
            distance_cost=base_time,
            landmark_cost=0.0,
            curvature_cost=base_time * (curvature_penalty - 1.0),
        )

    def _edge_heading(self, src: str, dst: str, forward: bool) -> float:
        meta = self.graph.get_edge_meta(src, dst)
        if meta and meta.geometry and len(meta.geometry) >= 2:
            points = meta.geometry
            if not forward:
                points = list(reversed(points))
            if not (meta.geom_from == src and meta.geom_to == dst):
                points = list(reversed(points))
            p1 = points[-2]
            p2 = points[-1]
            dx = p2["x"] - p1["x"]
            dy = p2["y"] - p1["y"]
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                return math.atan2(dy, dx)
        a = self.graph.nodes[src]
        b = self.graph.nodes[dst]
        return math.atan2(b.y - a.y, b.x - a.x)

    @staticmethod
    def _angle_diff_deg(a: float, b: float) -> float:
        diff = (b - a + math.pi) % (2.0 * math.pi) - math.pi
        return abs(math.degrees(diff))

    @staticmethod
    def _approx_turn_radius(in_cost: float, out_cost: float, angle_deg: float) -> Optional[float]:
        if angle_deg <= 1e-3:
            return None
        angle_rad = math.radians(angle_deg)
        base = min(in_cost, out_cost)
        if base <= 1e-3:
            return 0.0
        denom = max(1e-3, 2.0 * math.sin(angle_rad / 2.0))
        return base / denom
