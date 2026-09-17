from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from mvs.common.formula_utils import generate_neighbors, topology_total_cost
from mvs.scheduler.map_model import RoadGraph
from mvs.vehicle.path_constraints import KinematicPathPlanner, PathConstraintReport, VehicleKinematics


@dataclass
class HybridAStarConfig:
    step_m: float = 3.0
    yaw_resolution_deg: float = 10.0
    position_resolution_m: float = 3.0
    max_steer_deg: float = 28.0
    steering_samples: int = 7
    goal_pos_tolerance_m: float = 8.0
    goal_yaw_tolerance_deg: float = 35.0
    reverse_enabled: bool = False
    steering_change_penalty: float = 1.2
    gear_switch_penalty: float = 8.0
    corridor_margin_m: float = 3.0
    max_expansions: int = 30000


@dataclass
class TrajectoryPoint:
    x: float
    y: float
    yaw_deg: float
    steer_deg: float
    direction: int = 1

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "yaw_deg": round(self.yaw_deg, 3),
            "steer_deg": round(self.steer_deg, 3),
            "direction": self.direction,
        }


class HybridAStarPlanner:
    def __init__(self, graph: RoadGraph, limits: VehicleKinematics, cfg: HybridAStarConfig) -> None:
        self.graph = graph
        self.limits = limits
        self.cfg = cfg
        self.corridor_planner = KinematicPathPlanner(graph, limits)

    def plan(
        self,
        start_node: str,
        goal_node: str,
    ) -> Tuple[List[str], List[TrajectoryPoint], PathConstraintReport]:
        # 任务书“考虑车辆运动学约束的 A*”：先用拓扑走廊约束搜索空间，
        # 再用动作模型生成满足转向/轮距约束的连续轨迹点。
        node_path, coarse_report = self.corridor_planner.plan(start_node, goal_node)
        if not node_path:
            coarse_report.algorithm = "hybrid_a_star"
            return [], [], coarse_report

        corridor = self._build_corridor_polyline(node_path)
        if len(corridor) < 2:
            coarse_report.algorithm = "hybrid_a_star"
            return node_path, self._polyline_to_trajectory(corridor), coarse_report

        start_pt = self.graph.nodes[start_node]
        goal_pt = self.graph.nodes[goal_node]
        start_yaw = self._path_heading(node_path, 0)
        goal_yaw = self._path_heading(node_path, max(0, len(node_path) - 2))

        trajectory = self._search_hybrid_astar(
            corridor=corridor,
            start=(start_pt.x, start_pt.y, start_yaw),
            goal=(goal_pt.x, goal_pt.y, goal_yaw),
        )
        if not trajectory:
            coarse_report.feasible = False
            coarse_report.algorithm = "hybrid_a_star"
            coarse_report.violations.append("hybrid_astar_failed")
            return node_path, [], coarse_report

        report = self.corridor_planner.analyze_path(node_path)
        report.algorithm = "hybrid_a_star"
        report.estimated_travel_seconds = max(report.estimated_travel_seconds, self._trajectory_time_seconds(trajectory))
        return node_path, trajectory, report

    def plan_local_maneuver(
        self,
        start_pose: Tuple[float, float, float],
        goal_pose: Tuple[float, float, float],
        corridor: Sequence[Tuple[float, float, float]],
    ) -> List[TrajectoryPoint]:
        return self._search_hybrid_astar(corridor=corridor, start=start_pose, goal=goal_pose)

    def _search_hybrid_astar(
        self,
        corridor: Sequence[Tuple[float, float, float]],
        start: Tuple[float, float, float],
        goal: Tuple[float, float, float],
    ) -> List[TrajectoryPoint]:
        start_state = (start[0], start[1], self._normalize_angle(start[2]), 0.0, 1)
        start_key = self._state_key(*start_state[:3], direction=1)
        pq: List[Tuple[float, float, Tuple[float, float, float, float, int]]] = [
            (self._heuristic(start, goal), 0.0, start_state)
        ]
        costs: Dict[Tuple[int, int, int, int], float] = {start_key: 0.0}
        prev: Dict[Tuple[int, int, int, int], Optional[Tuple[int, int, int, int]]] = {start_key: None}
        pose_map: Dict[Tuple[int, int, int, int], TrajectoryPoint] = {
            start_key: TrajectoryPoint(start[0], start[1], math.degrees(start[2]), 0.0, 1)
        }

        expansions = 0
        while pq and expansions < self.cfg.max_expansions:
            _, g_cost, state = heapq.heappop(pq)
            x, y, yaw, prev_steer, direction = state
            key = self._state_key(x, y, yaw, direction)
            if g_cost > costs.get(key, float("inf")):
                continue
            if self._goal_reached((x, y, yaw), goal):
                return self._reconstruct_trajectory(key, prev, pose_map)

            expansions += 1
            # 公式(13) generate_neighbors 实际使用点：Hybrid A* 用动作集合生成下一批车辆状态。
            # 每个邻居都经过转向、方向和碰撞检查后，才可能进入搜索队列。
            for steer_deg, next_direction, nx, ny, nyaw in generate_neighbors(
                state=(x, y, yaw),
                actions=self._motion_primitives(),
                dt=self.cfg.step_m,
                simulate=self._simulate_motion_primitive,
            ):
                if not self._within_corridor(nx, ny, corridor):
                    continue

                nk = self._state_key(nx, ny, nyaw, next_direction)
                step_cost = self._motion_cost(steer_deg, prev_steer, next_direction, direction)
                nd = g_cost + step_cost
                if nd >= costs.get(nk, float("inf")):
                    continue
                costs[nk] = nd
                prev[nk] = key
                pose_map[nk] = TrajectoryPoint(nx, ny, math.degrees(nyaw), steer_deg, next_direction)
                heapq.heappush(pq, (nd + self._heuristic((nx, ny, nyaw), goal), nd, (nx, ny, nyaw, steer_deg, next_direction)))
        return []

    def _motion_primitives(self) -> List[Tuple[float, int]]:
        samples = max(3, self.cfg.steering_samples)
        max_steer = self.cfg.max_steer_deg
        if samples == 1:
            steer_values = [0.0]
        else:
            steer_values = [(-max_steer + (2 * max_steer) * i / (samples - 1)) for i in range(samples)]
        motions = [(s, 1) for s in steer_values]
        if self.cfg.reverse_enabled:
            motions.extend((s, -1) for s in steer_values)
        return motions

    def _propagate(self, x: float, y: float, yaw: float, steer_deg: float, direction: int) -> Tuple[float, float, float]:
        step = self.cfg.step_m * direction
        steer = math.radians(steer_deg)
        beta = math.tan(steer) / max(0.5, self.limits.wheelbase_m)
        nx = x + step * math.cos(yaw)
        ny = y + step * math.sin(yaw)
        nyaw = self._normalize_angle(yaw + step * beta)
        return nx, ny, nyaw

    def _simulate_motion_primitive(
        self,
        state: Tuple[float, float, float],
        action: Tuple[float, int],
        dt: float,
    ) -> Tuple[float, int, float, float, float]:
        steer_deg, direction = action
        nx, ny, nyaw = self._propagate(state[0], state[1], state[2], steer_deg, direction)
        return steer_deg, direction, nx, ny, nyaw

    def _motion_cost(self, steer_deg: float, prev_steer_deg: float, next_direction: int, prev_direction: int) -> float:
        distance_cost = self.cfg.step_m / max(0.5, self.limits.max_speed_mps or 8.0)
        steer_cost = abs(steer_deg) / max(1.0, self.cfg.max_steer_deg)
        steer_change = abs(steer_deg - prev_steer_deg) / max(1.0, self.cfg.max_steer_deg)
        gear_cost = self.cfg.gear_switch_penalty if next_direction != prev_direction else 0.0
        reverse_cost = 4.0 if next_direction < 0 else 0.0
        # 公式(12) topology_total_cost 实际使用点：Hybrid A* 单步动作代价。
        # 距离时间、转向幅度、转向变化、换挡和倒车惩罚共同决定搜索优先级。
        return topology_total_cost(
            climb_cost=0.0,
            distance_cost=distance_cost,
            landmark_cost=0.0,
            curvature_cost=steer_cost + steer_change * self.cfg.steering_change_penalty + gear_cost + reverse_cost,
        )

    def _goal_reached(self, state: Tuple[float, float, float], goal: Tuple[float, float, float]) -> bool:
        dist = math.hypot(state[0] - goal[0], state[1] - goal[1])
        yaw_err = abs(math.degrees(self._normalize_angle(state[2] - goal[2])))
        return dist <= self.cfg.goal_pos_tolerance_m and yaw_err <= self.cfg.goal_yaw_tolerance_deg

    def _heuristic(self, state: Tuple[float, float, float], goal: Tuple[float, float, float]) -> float:
        dist = math.hypot(state[0] - goal[0], state[1] - goal[1])
        yaw_penalty = abs(math.degrees(self._normalize_angle(state[2] - goal[2]))) / 90.0
        return dist / max(0.5, self.limits.max_speed_mps or 8.0) + yaw_penalty

    def _state_key(self, x: float, y: float, yaw: float, direction: int) -> Tuple[int, int, int, int]:
        pos_res = max(0.5, self.cfg.position_resolution_m)
        yaw_res = max(1.0, self.cfg.yaw_resolution_deg)
        return (
            int(round(x / pos_res)),
            int(round(y / pos_res)),
            int(round(math.degrees(self._normalize_angle(yaw)) / yaw_res)),
            direction,
        )

    def _build_corridor_polyline(self, node_path: List[str]) -> List[Tuple[float, float, float]]:
        polyline: List[Tuple[float, float, float]] = []
        for i in range(len(node_path) - 1):
            a, b = node_path[i], node_path[i + 1]
            meta = self.graph.get_edge_meta(a, b)
            if meta and meta.geometry:
                points = meta.geometry if (meta.geom_from == a and meta.geom_to == b) else list(reversed(meta.geometry))
                lane_width = meta.lane_width
                for p in points[:-1]:
                    polyline.append((float(p["x"]), float(p["y"]), lane_width))
            else:
                na = self.graph.nodes[a]
                nb = self.graph.nodes[b]
                lane_width = (meta.lane_width if meta else 8.0)
                polyline.append((na.x, na.y, lane_width))
                polyline.append((nb.x, nb.y, lane_width))
        if node_path:
            last = self.graph.nodes[node_path[-1]]
            polyline.append((last.x, last.y, self._path_lane_width(node_path[-2], node_path[-1]) if len(node_path) > 1 else 8.0))
        out: List[Tuple[float, float, float]] = []
        for pt in polyline:
            if not out or math.hypot(pt[0] - out[-1][0], pt[1] - out[-1][1]) > 1e-3:
                out.append(pt)
        return out

    def _path_heading(self, node_path: List[str], idx: int) -> float:
        if len(node_path) < 2:
            return 0.0
        a = node_path[idx]
        b = node_path[min(len(node_path) - 1, idx + 1)]
        na = self.graph.nodes[a]
        nb = self.graph.nodes[b]
        return math.atan2(nb.y - na.y, nb.x - na.x)

    def _path_lane_width(self, a: str, b: str) -> float:
        meta = self.graph.get_edge_meta(a, b)
        if meta is None:
            return 8.0
        return meta.lane_width

    def _within_corridor(self, x: float, y: float, corridor: Sequence[Tuple[float, float, float]]) -> bool:
        best_dist = float("inf")
        lane_width = 8.0
        for i in range(len(corridor) - 1):
            x1, y1, w1 = corridor[i]
            x2, y2, w2 = corridor[i + 1]
            dist = self._point_to_segment_distance(x, y, x1, y1, x2, y2)
            if dist < best_dist:
                best_dist = dist
                lane_width = 0.5 * (w1 + w2)
        allowance = lane_width / 2.0 + self.cfg.corridor_margin_m
        return best_dist <= allowance

    def _corridor_progress(self, x: float, y: float, corridor: Sequence[Tuple[float, float, float]]) -> float:
        progress = 0.0
        best_progress = 0.0
        best_dist = float("inf")
        for i in range(len(corridor) - 1):
            x1, y1, _ = corridor[i]
            x2, y2, _ = corridor[i + 1]
            seg_len = math.hypot(x2 - x1, y2 - y1)
            if seg_len <= 1e-6:
                continue
            t = self._segment_projection_ratio(x, y, x1, y1, x2, y2)
            px = x1 + (x2 - x1) * t
            py = y1 + (y2 - y1) * t
            dist = math.hypot(x - px, y - py)
            if dist < best_dist:
                best_dist = dist
                best_progress = progress + seg_len * t
            progress += seg_len
        return best_progress

    def _trajectory_time_seconds(self, trajectory: Sequence[TrajectoryPoint]) -> float:
        if len(trajectory) < 2:
            return 0.0
        total_dist = 0.0
        for i in range(len(trajectory) - 1):
            a = trajectory[i]
            b = trajectory[i + 1]
            total_dist += math.hypot(b.x - a.x, b.y - a.y)
        return total_dist / max(0.5, self.limits.max_speed_mps or 8.0)

    def _reconstruct_trajectory(
        self,
        goal_key: Tuple[int, int, int, int],
        prev: Dict[Tuple[int, int, int, int], Optional[Tuple[int, int, int, int]]],
        pose_map: Dict[Tuple[int, int, int, int], TrajectoryPoint],
    ) -> List[TrajectoryPoint]:
        path: List[TrajectoryPoint] = []
        cur: Optional[Tuple[int, int, int, int]] = goal_key
        while cur is not None:
            path.append(pose_map[cur])
            cur = prev.get(cur)
        path.reverse()
        return path

    @staticmethod
    def _segment_projection_ratio(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
        dx = x2 - x1
        dy = y2 - y1
        denom = dx * dx + dy * dy
        if denom <= 1e-6:
            return 0.0
        t = ((px - x1) * dx + (py - y1) * dy) / denom
        return min(1.0, max(0.0, t))

    @staticmethod
    def _point_to_segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
        t = HybridAStarPlanner._segment_projection_ratio(px, py, x1, y1, x2, y2)
        proj_x = x1 + (x2 - x1) * t
        proj_y = y1 + (y2 - y1) * t
        return math.hypot(px - proj_x, py - proj_y)

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        return (angle + math.pi) % (2.0 * math.pi) - math.pi

    @staticmethod
    def _polyline_to_trajectory(polyline: Sequence[Tuple[float, float, float]]) -> List[TrajectoryPoint]:
        out: List[TrajectoryPoint] = []
        for idx, (x, y, _) in enumerate(polyline):
            if idx + 1 < len(polyline):
                nx, ny, _ = polyline[idx + 1]
                yaw = math.degrees(math.atan2(ny - y, nx - x))
            elif out:
                yaw = out[-1].yaw_deg
            else:
                yaw = 0.0
            out.append(TrajectoryPoint(x=x, y=y, yaw_deg=yaw, steer_deg=0.0, direction=1))
        return out
