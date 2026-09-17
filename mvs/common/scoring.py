from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from mvs.common.formula_utils import (
    depot_busy_score,
    depot_prep_score,
    depot_time_score,
    depot_total_score,
    launch_point_cost,
    launcher_total_score,
    redundancy_score,
    safe_score,
    sigmoid_normalize,
    time_score,
)
from mvs.common.lane_graph import LaneGraph
from mvs.scheduler.map_model import RoadGraph, SpecialPoints


@dataclass
class DepotState:
    depot_id: str
    capacity: int = 1
    queue_count: int = 0
    occupied: bool = False
    wait_prep_sec: float = 0.0
    supported_ammo_types: Optional[List[str]] = None


@dataclass
class LaunchPointScoreWeights:
    distance: float = 0.30
    time_constraint: float = 0.25
    hide_support: float = 0.15
    launch_priority: float = 0.20
    redundancy: float = 0.10


@dataclass
class LaunchPointCostWeights:
    distance: float = 0.40
    hide: float = 0.30
    support: float = 0.30


def _node_distance(graph: RoadGraph, a: str, b: str) -> float:
    na = graph.nodes.get(a)
    nb = graph.nodes.get(b)
    if not na or not nb:
        return float("inf")
    return math.hypot(na.x - nb.x, na.y - nb.y)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_delta_seconds(later: Any, earlier: Any) -> Optional[float]:
    if later in {None, ""} or earlier in {None, ""}:
        return None
    try:
        return float(later) - float(earlier)
    except Exception:
        pass
    later_dt = _parse_ts(later)
    earlier_dt = _parse_ts(earlier)
    if later_dt and earlier_dt:
        return (later_dt - earlier_dt).total_seconds()
    return None


def vehicle_score(
    *,
    vehicle_id: str,
    current_node: str,
    launch_node: Optional[str],
    ammo_types: Iterable[str],
    required_ammo_type: Optional[str],
    speed_mps: float,
    health: float = 1.0,
    graph: Optional[RoadGraph] = None,
    lane_graph: Optional[LaneGraph] = None,
    request_time: Optional[str] = None,
    desired_fire_time: Optional[str] = None,
    fire_time_grace_sec: float = 15.0,
    ignore_speed_limits: bool = False,
) -> Dict[str, Any]:
    ammo_set = {str(x) for x in ammo_types}
    resource_score = 1.0 if not required_ammo_type or required_ammo_type in ammo_set else 0.0
    travel_sec = 0.0
    distance_m = 0.0
    feasible = True
    if graph and launch_node and current_node in graph.nodes and launch_node in graph.nodes:
        distance_m = _node_distance(graph, current_node, launch_node)
        if lane_graph:
            lane_ids, route = lane_graph.route_between_nodes(
                current_node,
                launch_node,
                speed_cap_mps=speed_mps,
                ignore_speed_limits=ignore_speed_limits,
            )
            feasible = bool(lane_ids or current_node == launch_node)
            travel_sec = float(route.travel_sec) if feasible else distance_m / max(speed_mps, 1e-6)
        else:
            travel_sec = distance_m / max(speed_mps, 1e-6)
    score_distance = max(0.0, 1.0 - travel_sec / 3600.0)
    score_health = max(0.0, min(1.0, health))
    timing_available_sec: Optional[float] = None
    timing_margin_sec: Optional[float] = None
    score_timing = 1.0
    timing_feasible = True
    timing_available_sec = _time_delta_seconds(desired_fire_time, request_time)
    if timing_available_sec is not None:
        timing_margin_sec = timing_available_sec - travel_sec
        timing_feasible = timing_margin_sec >= -max(0.0, float(fire_time_grace_sec))
        if timing_margin_sec >= 0:
            score_timing = min(1.0, 0.65 + min(timing_margin_sec, 1200.0) / 1200.0 * 0.35)
        else:
            score_timing = max(0.0, 0.65 - min(abs(timing_margin_sec), 600.0) / 600.0 * 0.65)
    if desired_fire_time:
        score_total = 100.0 * (
            0.30 * score_distance
            + 0.25 * resource_score
            + 0.15 * score_health
            + 0.30 * score_timing
        )
    else:
        score_total = 100.0 * (0.45 * score_distance + 0.35 * resource_score + 0.20 * score_health)
    return {
        "vehicle_id": vehicle_id,
        "launch_node": launch_node,
        "current_node": current_node,
        "required_ammo_type": required_ammo_type,
        "feasible": bool(feasible and resource_score > 0.0 and timing_feasible),
        "distance_m": round(distance_m, 3),
        "travel_sec": round(travel_sec, 3),
        "score_distance": round(score_distance * 100.0, 3),
        "score_resource": round(resource_score * 100.0, 3),
        "score_health": round(score_health * 100.0, 3),
        "score_timing": round(score_timing * 100.0, 3),
        "timing_available_sec": round(timing_available_sec, 3) if timing_available_sec is not None else None,
        "timing_margin_sec": round(timing_margin_sec, 3) if timing_margin_sec is not None else None,
        "timing_feasible": bool(timing_feasible),
        "score_total": round(score_total, 3),
        "ranking_note": "车辆评分用于模型/节点查询；调度器内部同类评分参与车辆-发射点候选排序",
    }


def compute_dispatch_priority(
    *,
    task_deadline_sec: Optional[float],
    current_time_sec: Optional[float],
    vehicle_type_weight: float,
    distance_to_target_m: Optional[float],
    eta_1: float = 1.0,
    eta_2: float = 1.0,
    eta_3: float = 1.0,
) -> Dict[str, Any]:
    """任务书中的车辆优先级函数 P(i)。

    这里不直接改调度流程，只把优先级计算显式化，便于后续在调度协同/
    冲突消解阶段复用。
    """
    # 对应公式(5)：车辆优先级由时间紧迫性、车辆类型权重和距离项组成。
    # 这里输出拆分项，方便日志和候选上下文里解释“为什么某辆车优先”。
    slack_sec = None
    if task_deadline_sec is not None and current_time_sec is not None:
        slack_sec = float(task_deadline_sec) - float(current_time_sec)
    time_term = 0.0
    if slack_sec is not None:
        time_term = 1.0 / max(1.0, slack_sec)
    distance_term = 0.0
    if distance_to_target_m not in {None, float("inf")}:
        try:
            distance_term = 1.0 / max(1.0, float(distance_to_target_m))
        except Exception:
            distance_term = 0.0
    priority = (
        float(eta_1) * time_term
        + float(eta_2) * float(vehicle_type_weight)
        + float(eta_3) * distance_term
    )
    return {
        "priority_total": round(priority, 9),
        "priority_time_term": round(time_term, 9),
        "priority_vehicle_type_term": round(float(vehicle_type_weight), 9),
        "priority_distance_term": round(distance_term, 9),
        "slack_sec": round(slack_sec, 3) if slack_sec is not None else None,
        "eta_1": float(eta_1),
        "eta_2": float(eta_2),
        "eta_3": float(eta_3),
        "formula": "eta_1/(deadline-current)+eta_2*type(i)+eta_3/d(i,target)",
    }


def score_launch_point_for_vehicle(
    *,
    vehicle_id: str,
    current_node: str,
    launch_node: str,
    ammo_types: Iterable[str],
    required_ammo_type: Optional[str],
    speed_mps: float,
    graph: Optional[RoadGraph] = None,
    lane_graph: Optional[LaneGraph] = None,
    request_time: Optional[Any] = None,
    desired_arrival_time: Optional[Any] = None,
    fire_time_grace_sec: float = 15.0,
    hide_candidate_count: int = 0,
    launch_priority_score: float = 100.0,
    redundancy_ratio: float = 1.0,
    weights: Optional[LaunchPointScoreWeights] = None,
    formula_cost_weights: Optional[LaunchPointCostWeights] = None,
    distance_to_hide_m: Optional[float] = None,
    support_score: Optional[float] = None,
    ignore_speed_limits: bool = False,
) -> Dict[str, Any]:
    """任务书语义下的“发射点优选排序评分”。

    不改变现有候选点生成主流程，只把当前已经存在的距离/时间/隐蔽点/冗余
    信息统一折算成任务书里更容易对照的评分结构。
    """
    # 对应公式(1)和发射点候选评分：先复用原有车辆到发射点可达性评分。
    # 再补充隐蔽点支撑、发射点优先级和冗余路线分，作为调度发给模型的解释字段。
    weights = weights or LaunchPointScoreWeights()
    formula_cost_weights = formula_cost_weights or LaunchPointCostWeights()
    base = vehicle_score(
        vehicle_id=vehicle_id,
        current_node=current_node,
        launch_node=launch_node,
        ammo_types=ammo_types,
        required_ammo_type=required_ammo_type,
        speed_mps=speed_mps,
        graph=graph,
        lane_graph=lane_graph,
        request_time=request_time,
        desired_fire_time=desired_arrival_time,
        fire_time_grace_sec=fire_time_grace_sec,
        ignore_speed_limits=ignore_speed_limits,
    )
    score_distance = float(base.get("score_distance", 0.0) or 0.0)
    score_time_constraint = float(base.get("score_timing", 0.0) or 0.0)
    score_hide_support = min(100.0, 40.0 + 15.0 * max(0, int(hide_candidate_count)))
    score_launch_priority = max(0.0, min(100.0, float(launch_priority_score)))
    score_support = max(0.0, min(100.0, float(support_score if support_score is not None else score_launch_priority)))
    ratio = max(1.0, float(redundancy_ratio))
    score_redundancy = max(0.0, min(100.0, 100.0 / ratio))
    # 发射点多维评分实际计算点：距离、时间约束、隐蔽支撑、点位优先级、冗余能力。
    # 该 score_total 会进入调度端候选发射点排序和发给模型的 launch_score_breakdown。
    base_score_total = (
        weights.distance * score_distance
        + weights.time_constraint * score_time_constraint
        + weights.hide_support * score_hide_support
        + weights.launch_priority * score_launch_priority
        + weights.redundancy * score_redundancy
    )
    # 任务书“保障/支撑评分”实际接入点：
    # support_weight 越大，发射点自身保障支撑分对候选排序影响越明显。
    support_weight = max(0.0, min(1.0, float(formula_cost_weights.support)))
    score_total = (1.0 - support_weight) * base_score_total + support_weight * score_support
    formula_cost = None
    if distance_to_hide_m is not None or support_score is not None:
        # 公式(1) launch_point_cost 实际计算点：保留任务书原式的发射点代价。
        formula_cost = launch_point_cost(
            distance_to_vehicle=float(base.get("distance_m", 0.0) or 0.0),
            distance_to_hide=float(distance_to_hide_m or 0.0),
            support_score=float(support_score if support_score is not None else score_launch_priority),
            w_distance=formula_cost_weights.distance,
            w_hide=formula_cost_weights.hide,
            w_support=formula_cost_weights.support,
        )
    return {
        **base,
        "score_launch_distance": round(score_distance, 3),
        "score_launch_time_constraint": round(score_time_constraint, 3),
        "score_launch_hide_support": round(score_hide_support, 3),
        "score_launch_priority": round(score_launch_priority, 3),
        "score_launch_redundancy": round(score_redundancy, 3),
        "score_launch_support": round(score_support, 3),
        "score_launch_support_weight": round(support_weight, 6),
        "score_total": round(score_total, 3),
        "formula_launch_point_cost": round(formula_cost, 3) if formula_cost is not None else None,
        "formula_launch_point_cost_semantics": "w1*distance_to_vehicle+w2*distance_to_hide-w3*support_score",
        "score_semantics": "task_book_launch_point_priority",
    }


def score_vehicle_task_execution(
    *,
    vehicle_id: str,
    current_node: str,
    launch_node: Optional[str],
    ammo_types: Iterable[str],
    required_ammo_type: Optional[str],
    speed_mps: float,
    graph: Optional[RoadGraph] = None,
    lane_graph: Optional[LaneGraph] = None,
    request_time: Optional[Any] = None,
    desired_fire_time: Optional[Any] = None,
    fire_time_grace_sec: float = 15.0,
    wait_seconds: float = 0.0,
    launch_prepare_seconds: float = 0.0,
    hide_selected: bool = False,
    fire_time_error_sec: Optional[float] = None,
    ignore_speed_limits: bool = False,
    redundant_estimated_times: Optional[Iterable[float]] = None,
    redundant_time_weights: Optional[Iterable[float]] = None,
) -> Dict[str, Any]:
    """任务书语义下的车辆任务执行评分。"""
    # 发射平台任务执行评分主入口：
    # 先复用 vehicle_score 得到路径耗时/时间窗口，再按任务书公式(16)~(21)
    # 计算时间评分、安全评分、路线冗余评分和最终总评分。
    base = vehicle_score(
        vehicle_id=vehicle_id,
        current_node=current_node,
        launch_node=launch_node,
        ammo_types=ammo_types,
        required_ammo_type=required_ammo_type,
        speed_mps=speed_mps,
        graph=graph,
        lane_graph=lane_graph,
        request_time=request_time,
        desired_fire_time=desired_fire_time,
        fire_time_grace_sec=fire_time_grace_sec,
        ignore_speed_limits=ignore_speed_limits,
    )
    maneuver_score = float(base.get("score_distance", 0.0) or 0.0)
    timing_score = float(base.get("score_timing", 0.0) or 0.0)
    wait_score = max(0.0, 100.0 - min(100.0, float(wait_seconds) / 18.0))
    prepare_score = max(0.0, 100.0 - min(100.0, float(launch_prepare_seconds) / 18.0))
    conceal_score = 100.0 if hide_selected else 55.0
    penalty = 0.0
    if fire_time_error_sec is not None:
        try:
            error = abs(float(fire_time_error_sec))
            penalty = min(60.0, error / 10.0)
        except Exception:
            penalty = 0.0
    travel_plus_wait_sec = float(base.get("travel_sec", 0.0) or 0.0) + float(wait_seconds)
    timing_available = base.get("timing_available_sec")
    formula_time_score = None
    formula_safe_score = None
    formula_redundancy_score = None
    formula_time_score_norm = None
    formula_safe_score_norm = None
    formula_redundancy_score_norm = None
    if timing_available not in {None, 0}:
        # 公式(16) time_score 实际计算点：
        # 用神经网络/路网 ETA 得到的预计耗时与任务最晚可用时间做对数型评价。
        formula_time_score = time_score(travel_plus_wait_sec, float(timing_available))
        # 公式(17) safe_score 实际计算点：
        # 根据是否超出时间窗、隐蔽等待/发射准备时间估算暴露风险，再转成安全完成概率。
        formula_safe_score = safe_score(
            travel_plus_wait_sec,
            float(timing_available),
            max(1.0, float(wait_seconds) if wait_seconds else float(launch_prepare_seconds) or 1.0),
        )
        red_times = [float(x) for x in (redundant_estimated_times or []) if x not in {None, ""}]
        if not red_times:
            red_times = [travel_plus_wait_sec]
        red_weights = [float(x) for x in (redundant_time_weights or []) if x not in {None, ""}]
        if len(red_weights) < len(red_times):
            red_weights.extend([1.0] * (len(red_times) - len(red_weights)))
        weight_sum = sum(red_weights) or 1.0
        red_weights = [w / weight_sum for w in red_weights[: len(red_times)]]
        # 公式(18)(19) redundancy_score 实际计算点：
        # 候选/备用发射点 ETA 按权重合成为冗余时间，再与任务时间窗比较。
        # 未显式传入备用 ETA 时，使用当前候选路径 ETA，保证评分公式始终在主流程生效。
        formula_redundancy_score = redundancy_score(red_times, red_weights, float(timing_available))
        # 公式(20) Sigmoid 归一化实际计算点：
        # 时间分和冗余分是对数值，先压到 0~1；安全分本身就是概率，按任务书直接使用。
        formula_time_score_norm = sigmoid_normalize(formula_time_score, k=3.0, s0=0.0)
        formula_redundancy_score_norm = sigmoid_normalize(formula_redundancy_score, k=3.0, s0=0.0)
        formula_safe_score_norm = max(0.0, min(1.0, float(formula_safe_score)))

    # 保留旧工程分，便于对比历史测试结果；不再作为 score_total 主口径。
    engineering_score_total = (
        0.35 * maneuver_score
        + 0.25 * timing_score
        + 0.10 * wait_score
        + 0.10 * prepare_score
        + 0.20 * conceal_score
        - penalty
    )
    if formula_time_score_norm is not None and formula_safe_score_norm is not None and formula_redundancy_score_norm is not None:
        # 公式(21) launcher_total_score 实际计算点：
        # 发射平台最终任务执行评分由时间评分、路线冗余评分、安全评分加权得到。
        score_total = 100.0 * launcher_total_score(
            formula_time_score_norm,
            formula_redundancy_score_norm,
            formula_safe_score_norm,
        )
        score_source = "task_book_formula_16_21"
    else:
        # 没有时间窗口时无法计算公式(16)(19)，才退回旧工程分，避免无任务时间输入时中断。
        score_total = engineering_score_total
        score_source = "engineering_fallback_missing_timing_window"
    return {
        **base,
        "score_maneuver": round(maneuver_score, 3),
        "score_time_constraint": round(timing_score, 3),
        "score_waiting": round(wait_score, 3),
        "score_launch_prepare": round(prepare_score, 3),
        "score_hide_action": round(conceal_score, 3),
        "score_penalty_fire_time_error": round(penalty, 3),
        "formula_time_score": round(formula_time_score, 6) if formula_time_score is not None else None,
        "formula_safe_score": round(formula_safe_score, 6) if formula_safe_score is not None else None,
        "formula_redundancy_score": round(formula_redundancy_score, 6) if formula_redundancy_score is not None else None,
        "formula_time_score_norm": round(formula_time_score_norm, 6) if formula_time_score_norm is not None else None,
        "formula_safe_score_norm": round(formula_safe_score_norm, 6) if formula_safe_score_norm is not None else None,
        "formula_redundancy_score_norm": round(formula_redundancy_score_norm, 6) if formula_redundancy_score_norm is not None else None,
        "score_engineering_total": round(max(0.0, min(100.0, engineering_score_total)), 3),
        "score_total": round(max(0.0, min(100.0, score_total)), 3),
        "score_source": score_source,
        "score_semantics": "task_book_vehicle_task_execution",
    }


def score_depot_task_execution(
    *,
    depot_score_row: Dict[str, Any],
    success_rate_score: float = 100.0,
) -> Dict[str, Any]:
    # 对应公式(22)~(26)：将贮备库位置、繁忙度、等待、资源和后续任务支撑统一输出。
    # 这层不改变原始评分来源，只额外给出 formula_* 字段供验收对照。
    location_score = float(depot_score_row.get("score_travel", 0.0) or 0.0)
    busy_score = float(depot_score_row.get("score_busy", 0.0) or 0.0)
    wait_score = float(depot_score_row.get("score_wait", 0.0) or 0.0)
    resource_score = float(depot_score_row.get("score_resource", 0.0) or 0.0)
    next_score = float(depot_score_row.get("score_next", 0.0) or 0.0)
    queue_count = int(depot_score_row.get("queue_count", 0) or 0)
    occupied_count = 1 if depot_score_row.get("occupied") else 0
    capacity = int(depot_score_row.get("capacity", 16) or 16)
    wait_sec = float(depot_score_row.get("estimated_wait_sec", 0.0) or 0.0)
    travel_sec = float(depot_score_row.get("travel_sec_from_launch", 0.0) or 0.0)
    # 公式(22) depot_busy_score 实际计算点：根据队列/占用和容量生成繁忙度解释分。
    formula_busy_score = depot_busy_score(queue_count + occupied_count, capacity)
    # 公式(23) depot_prep_score 实际计算点：把补给等待/准备时间转为负向惩罚。
    formula_prep_score = depot_prep_score(wait_sec, 3600.0)
    # 公式(25)+(20) depot_time_score/sigmoid_normalize 实际计算点：
    # 用发射点到库的到达时间形成区位时间评分，并压到 0~1。
    formula_time_score_norm = sigmoid_normalize(depot_time_score([travel_sec], 3600.0))
    # 公式(26) depot_total_score 实际计算点：汇总繁忙、准备等待和区位时间。
    # 该值作为 formula_depot_total_score 输出，用于解释；当前真实全局分配另走矩阵分数。
    formula_total = depot_total_score(
        formula_busy_score,
        formula_prep_score,
        formula_time_score_norm,
    )
    # 贮备库任务书包装评分实际计算点：位置、繁忙、等待、资源、后续衔接和成功率加权。
    # 这是旧式查询/解释口径的 score_total，不等同于两阶段矩阵分配的 a*距离+b*排名+c*负载。
    total = (
        0.25 * location_score
        + 0.20 * busy_score
        + 0.20 * wait_score
        + 0.20 * resource_score
        + 0.05 * next_score
        + 0.10 * float(success_rate_score)
    )
    return {
        **depot_score_row,
        "score_depot_location": round(location_score, 3),
        "score_depot_busy": round(busy_score, 3),
        "score_depot_wait": round(wait_score, 3),
        "score_depot_resource": round(resource_score, 3),
        "score_depot_next_step": round(next_score, 3),
        "score_depot_success_rate": round(float(success_rate_score), 3),
        "formula_depot_busy_score": round(formula_busy_score, 6),
        "formula_depot_prep_score": round(formula_prep_score, 6),
        "formula_depot_time_score_norm": round(formula_time_score_norm, 6),
        "formula_depot_total_score": round(formula_total, 6),
        "score_total": round(max(0.0, min(100.0, total)), 3),
        "score_semantics": "task_book_depot_task_execution",
    }


class DepotScorer:
    def __init__(
        self,
        graph: RoadGraph,
        points: SpecialPoints,
        lane_graph: Optional[LaneGraph] = None,
        reload_duration_sec: float = 60.0,
        default_capacity: int = 1,
    ) -> None:
        self.graph = graph
        self.points = points
        self.lane_graph = lane_graph or LaneGraph(graph)
        self.reload_duration_sec = float(reload_duration_sec)
        self.default_capacity = max(1, int(default_capacity))

    def score_depots(
        self,
        *,
        launch_node: str,
        vehicle_id: Optional[str] = None,
        ammo_type: Optional[str] = None,
        vehicle_node: Optional[str] = None,
        speed_mps: float = 8.0,
        depot_states: Optional[Dict[str, DepotState]] = None,
        candidate_depots: Optional[Iterable[str]] = None,
        next_node: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 贮备库局部评分入口：从某个发射点出发，逐个库计算路网距离、等待和资源匹配。
        # 结果既用于旧式贮备库推荐，也会被包装成任务书公式(22)~(26)的解释项。
        depot_states = depot_states or {}
        candidates = list(candidate_depots or self.points.depots)
        scored: List[Dict[str, Any]] = []
        for depot_id in candidates:
            if depot_id not in self.graph.nodes or launch_node not in self.graph.nodes:
                continue
            state = depot_states.get(depot_id, DepotState(depot_id=depot_id, capacity=self.default_capacity))
            route_from_launch = self._route(launch_node, depot_id, speed_mps)
            wait_sec = self._estimated_wait_sec(state)
            supported = state.supported_ammo_types
            resource_ok = ammo_type is None or not supported or ammo_type in set(supported)
            next_leg = self._route(depot_id, next_node, speed_mps) if next_node else {"travel_sec": 0.0, "distance_m": 0.0, "feasible": True}
            busy_ratio = min(1.0, max(0.0, (state.queue_count + (1 if state.occupied else 0)) / max(1, state.capacity)))
            score_travel = max(0.0, 1.0 - route_from_launch["travel_sec"] / 3600.0)
            score_wait = max(0.0, 1.0 - wait_sec / 1800.0)
            score_busy = max(0.0, 1.0 - busy_ratio)
            score_resource = 1.0 if resource_ok else 0.0
            score_next = max(0.0, 1.0 - next_leg["travel_sec"] / 3600.0)
            total = 100.0 * (
                0.35 * score_travel
                + 0.20 * score_wait
                + 0.20 * score_busy
                + 0.15 * score_resource
                + 0.10 * score_next
            )
            scored.append(
                {
                    "depot_id": depot_id,
                    "depot_node": depot_id,
                    "vehicle_id": vehicle_id,
                    "launch_node": launch_node,
                    "ammo_type": ammo_type,
                    "feasible": bool(route_from_launch["feasible"] and resource_ok),
                    "travel_sec_from_launch": round(route_from_launch["travel_sec"], 3),
                    "distance_m_from_launch": round(route_from_launch["distance_m"], 3),
                    "estimated_wait_sec": round(wait_sec, 3),
                    "reload_sec": round(self.reload_duration_sec, 3),
                    "queue_count": int(state.queue_count),
                    "capacity": int(state.capacity),
                    "occupied": bool(state.occupied),
                    "score_travel": round(score_travel * 100.0, 3),
                    "score_wait": round(score_wait * 100.0, 3),
                    "score_busy": round(score_busy * 100.0, 3),
                    "score_resource": round(score_resource * 100.0, 3),
                    "score_next": round(score_next * 100.0, 3),
                    "score_total": round(total, 3),
                }
            )
        scored.sort(key=lambda row: (not bool(row["feasible"]), -float(row["score_total"]), str(row["depot_id"])))
        return {
            "vehicle_id": vehicle_id,
            "launch_node": launch_node,
            "ammo_type": ammo_type,
            "depot_scores": scored,
            "selected_depot": scored[0]["depot_id"] if scored else None,
            "ranking_note": "贮备库评分是发射点/车辆/任务相关的动态评分，不是库点全局固定分",
        }

    def _estimated_wait_sec(self, state: DepotState) -> float:
        wait = max(0.0, float(state.wait_prep_sec))
        wait += max(0, int(state.queue_count)) * self.reload_duration_sec
        if state.occupied:
            wait += self.reload_duration_sec
        return wait

    def _route(self, start: str, end: Optional[str], speed_mps: float) -> Dict[str, Any]:
        if not end or start not in self.graph.nodes or end not in self.graph.nodes:
            return {"travel_sec": 0.0, "distance_m": 0.0, "feasible": False}
        if start == end:
            return {"travel_sec": 0.0, "distance_m": 0.0, "feasible": True}
        lane_ids, route = self.lane_graph.route_between_nodes(start, end, speed_cap_mps=speed_mps)
        if lane_ids:
            return {"travel_sec": float(route.travel_sec), "distance_m": float(route.distance_m), "feasible": True}
        dist = _node_distance(self.graph, start, end)
        return {"travel_sec": dist / max(speed_mps, 1e-6), "distance_m": dist, "feasible": False}
