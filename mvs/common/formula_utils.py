from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Protocol, Sequence


def _positive(value: float, eps: float = 1e-6) -> float:
    return max(float(value), eps)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# 公式(1)：区域发射/任务点综合代价，越小越优。
# 用车辆到发射点距离、发射点到隐蔽点距离和保障支撑分数做线性组合。
# 这里是任务书公式的纯函数版本，业务侧会把当前路网距离和候选点属性传进来。
def launch_point_cost(
    distance_to_vehicle: float,
    distance_to_hide: float,
    support_score: float,
    w_distance: float = 0.4,
    w_hide: float = 0.3,
    w_support: float = 0.3,
) -> float:
    return (
        float(w_distance) * float(distance_to_vehicle)
        + float(w_hide) * float(distance_to_hide)
        - float(w_support) * float(support_score)
    )


# 公式(2)：时间窗交叠长度。
# 冲突消解里先把车辆轨迹离散到时间窗，再用交叠长度判断是否同时占用。
# 返回 0 表示两个时间窗不冲突，大于 0 表示存在时间重叠。
def time_window_overlap(start_i: float, end_i: float, start_j: float, end_j: float) -> float:
    return max(0.0, min(float(end_i), float(end_j)) - max(float(start_i), float(start_j)))


def has_route_time_conflict(start_i: float, end_i: float, start_j: float, end_j: float) -> bool:
    return time_window_overlap(start_i, end_i, start_j, end_j) > 0.0


def has_segment_capacity_conflict(active_count: int, capacity: int) -> bool:
    return capacity_delta(active_count, capacity) > 0


def capacity_delta(selected_count: int, capacity: int) -> int:
    return int(selected_count) - int(capacity)


# 公式(3)：发射点容量差。
# 表示某个发射点被选择数量与容量上限的差值。
# 大于 0 时说明超过容量，需要调度侧换点或冲突修复。
def launch_point_capacity_delta(selected_count: int, capacity: int) -> int:
    return capacity_delta(selected_count, capacity)


# 公式(4)：隐蔽点容量差。
# 与发射点容量差一致，只是对象换成隐蔽待机点。
# 当前流程用它描述候选隐蔽点是否被过量占用。
def hide_point_capacity_delta(selected_count: int, capacity: int) -> int:
    return capacity_delta(selected_count, capacity)


# 公式(5)：车辆优先级，越大越优。
# 综合任务剩余时间、车辆类型权重和到目标距离，生成调度优先级。
# 分数越高表示越应该优先进入车辆-任务匹配或冲突消解决策。
def vehicle_priority(
    deadline: float,
    current_time: float,
    vehicle_type_weight: float,
    distance_to_target: float,
    eta_1: float = 1.0,
    eta_2: float = 1.0,
    eta_3: float = 1.0,
    eps: float = 1e-6,
) -> float:
    return (
        float(eta_1) / _positive(float(deadline) - float(current_time), eps)
        + float(eta_2) * float(vehicle_type_weight)
        + float(eta_3) / _positive(distance_to_target, eps)
    )


# 公式(6)：贮备库槽位容量差。
# 用于表示某个贮备库当前分配车辆数是否超过 capacity。
# 后续全局分配会在容量归零时移除该库，避免继续分配。
def depot_slot_delta(selected_count: int, capacity: int) -> int:
    return capacity_delta(selected_count, capacity)


# 公式(7)：车辆与贮备库资源类型不匹配。
# 返回 0/1 的硬约束标志，0 表示资源类型匹配。
# 可以作为贮备库评分惩罚项，防止车型或补给资源不兼容。
def vehicle_depot_type_mismatch(vehicle_resource_type: Any, depot_resource_type: Any) -> int:
    return 0 if str(vehicle_resource_type) == str(depot_resource_type) else 1


@dataclass(frozen=True)
class GridFeature:
    length_m: float
    elevation_in: float = 0.0
    elevation_out: float = 0.0
    heading_in_rad: float = 0.0
    heading_out_rad: float = 0.0
    extra_features: Sequence[float] = ()


class VelocityModel(Protocol):
    def predict(self, features: Sequence[float]) -> float:
        ...


def grid_features_from_polyline(
    polyline: Sequence[Sequence[float]],
    *,
    grid_length_m: float = 20.0,
    elevations: Sequence[float] = (),
    extra_features: Sequence[float] = (),
) -> List[GridFeature]:
    """
    将路径中心线按固定距离切成任务书口径的等距栅格。

    输入路径折线，输出 GridFeature 列表，后续可交给 estimate_eta_by_grid_model
    逐格调用速度/神经网络模型并累计 ETA。
    """
    step = max(0.1, float(grid_length_m))
    pts = [(float(p[0]), float(p[1])) for p in polyline if len(p) >= 2]
    if len(pts) < 2:
        return []
    elev = [float(x) for x in elevations]
    if len(elev) < len(pts):
        elev.extend([0.0] * (len(pts) - len(elev)))

    cumulative = [0.0]
    headings: List[float] = []
    for idx in range(len(pts) - 1):
        x1, y1 = pts[idx]
        x2, y2 = pts[idx + 1]
        seg_len = math.hypot(x2 - x1, y2 - y1)
        cumulative.append(cumulative[-1] + seg_len)
        headings.append(math.atan2(y2 - y1, x2 - x1) if seg_len > 1e-9 else (headings[-1] if headings else 0.0))
    total_len = cumulative[-1]
    if total_len <= 1e-9:
        return []

    def sample_at(distance_m: float) -> tuple[float, float, float, float]:
        d = max(0.0, min(float(distance_m), total_len))
        for idx in range(len(pts) - 1):
            if d <= cumulative[idx + 1] or idx == len(pts) - 2:
                seg_len = max(1e-9, cumulative[idx + 1] - cumulative[idx])
                ratio = (d - cumulative[idx]) / seg_len
                x1, y1 = pts[idx]
                x2, y2 = pts[idx + 1]
                z1 = elev[idx]
                z2 = elev[idx + 1]
                return (
                    x1 + (x2 - x1) * ratio,
                    y1 + (y2 - y1) * ratio,
                    z1 + (z2 - z1) * ratio,
                    headings[idx],
                )
        x, y = pts[-1]
        return x, y, elev[-1], headings[-1] if headings else 0.0

    grids: List[GridFeature] = []
    start_d = 0.0
    while start_d < total_len - 1e-9:
        end_d = min(total_len, start_d + step)
        _x1, _y1, z1, h1 = sample_at(start_d)
        _x2, _y2, z2, h2 = sample_at(end_d)
        grids.append(
            GridFeature(
                length_m=end_d - start_d,
                elevation_in=z1,
                elevation_out=z2,
                heading_in_rad=h1,
                heading_out_rad=h2,
                extra_features=tuple(float(x) for x in extra_features),
            )
        )
        start_d = end_d
    return grids


# 公式(8)：路段曲率特征。
# 用进入/离开方向角变化除以路段长度，形成神经网络 ETA 的几何输入。
# 主流程中 VehicleApp 会从路网边元数据读取 curvature 并传入 MLPTimePredictor。
# 曲率越大，通常意味着转弯/机动成本越高。
def road_curvature(heading_in_rad: float, heading_out_rad: float, length_m: float, eps: float = 1e-6) -> float:
    return abs(float(heading_out_rad) - float(heading_in_rad)) / _positive(length_m, eps)


# 公式(9)：路段坡度特征。
# 用高程差除以路段长度，描述上坡/下坡对行驶时间的影响。
# 当前地图高程不足时 VehicleApp 传入 grade=0，相当于平路特征，不影响原流程。
def road_grade(elevation_in: float, elevation_out: float, length_m: float, eps: float = 1e-6) -> float:
    return (float(elevation_out) - float(elevation_in)) / _positive(length_m, eps)


def clip_grid_exit_speed(speed_mps: float, min_speed_mps: float = 0.1, max_speed_mps: float = 50.0) -> float:
    return max(float(min_speed_mps), min(float(max_speed_mps), float(speed_mps)))


# 公式(10)：栅格耗时。
# 采用进出速度的调和式近似，计算单个栅格/路段的行驶时间。
# MLPTimePredictor 若输出出口速度，则用同一形式把速度结果转成秒级 ETA。
def grid_delta_time(length_m: float, speed_in_mps: float, speed_out_mps: float, eps: float = 1e-6) -> float:
    return 2.0 * float(length_m) / _positive(float(speed_in_mps) + float(speed_out_mps), eps)


# 公式(11)：神经网络/外部速度模型 ETA 汇总。
# 对整条路径逐段预测出口速度，再累加每段 grid_delta_time。

def estimate_eta_by_grid_model(
    grids: Iterable[GridFeature],
    velocity_model: VelocityModel,
    initial_speed_mps: float,
    min_speed_mps: float = 0.1,
    max_speed_mps: float = 50.0,
) -> float:
    total = 0.0
    speed_in = clip_grid_exit_speed(initial_speed_mps, min_speed_mps, max_speed_mps)
    for grid in grids:
        features: List[float] = [
            float(grid.length_m),
            road_curvature(grid.heading_in_rad, grid.heading_out_rad, grid.length_m),
            road_grade(grid.elevation_in, grid.elevation_out, grid.length_m),
            float(speed_in),
            float(grid.elevation_in),
            float(grid.elevation_out),
        ]
        features.extend(float(x) for x in grid.extra_features)
        speed_out = clip_grid_exit_speed(
            velocity_model.predict(features),
            min_speed_mps,
            max_speed_mps,
        )
        total += grid_delta_time(grid.length_m, speed_in, speed_out)
        speed_in = speed_out
    return total


# 公式(12)：拓扑搜索综合代价，越小越优。
# 将爬坡、距离、地标偏好和曲率等代价合成为路径搜索代价。
# 当前可作为 A*/Hybrid A* 等搜索器的统一代价接口。
def topology_total_cost(
    climb_cost: float,
    distance_cost: float,
    landmark_cost: float,
    curvature_cost: float,
    w_climb: float = 1.0,
    w_distance: float = 1.0,
    w_landmark: float = 1.0,
    w_curvature: float = 1.0,
) -> float:
    return (
        float(w_climb) * float(climb_cost)
        + float(w_distance) * float(distance_cost)
        + float(w_landmark) * float(landmark_cost)
        + float(w_curvature) * float(curvature_cost)
    )


# 公式(13)：动作模型邻居生成。
# 根据动作集合和仿真步长生成下一批候选状态。
# 这里保持为通用接口，具体车辆运动模型由调用方传入 simulate。
def generate_neighbors(
    state: Any,
    actions: Iterable[Any],
    dt: float,
    simulate: Callable[[Any, Any, float], Any],
) -> List[Any]:
    return [simulate(state, action, float(dt)) for action in actions]


# 公式(14)：卫星周期开始前的隐蔽点搜索范围。
# 根据最大可用时间、已行驶/等待时间和最大速度估算隐蔽点搜索半径。
# 目的是控制候选隐蔽点范围，避免搜索过宽导致耗时过大。
def hide_search_distance_before(
    d_min: float,
    t_max: float,
    t_driving: float,
    t_waiting: float,
    v_max: float,
    satellite_pass_count: int,
    eps: float = 1e-6,
) -> float:
    span = (float(t_max) - float(t_driving) - float(t_waiting)) * 0.8 * float(v_max)
    return max(float(d_min), span / _positive(float(satellite_pass_count), eps))


def hide_search_distance_after(d_start: float, d_end: float) -> float:
    return max(0.0, float(d_end) - float(d_start))


# 公式(16)：发射平台时间评分。
# 用预计耗时与最大可用时间的比值计算时间维度评分。
# 默认采用“时间越短分越高”的工程修正版，可用 doc_formula 切回原式。
def time_score(estimated_time: float, max_time: float, doc_formula: bool = False, eps: float = 1e-6) -> float:
    numerator = estimated_time if doc_formula else max_time
    denominator = max_time if doc_formula else estimated_time
    return math.log(_positive(numerator, eps) / _positive(denominator, eps))


# 公式(17)：暴露概率和安全评分。
# 根据超时量、隐蔽等待时间和风险系数估计暴露概率。
# safe_score 是 1-p，供车辆任务执行评分输出可解释安全分。
def exposure_probability(
    estimated_time: float,
    max_time: float,
    hide_time: float,
    tau: float = 1.0,
    doc_formula: bool = False,
    eps: float = 1e-6,
) -> float:
    overtime = min(float(estimated_time) - float(max_time), 0.0) if doc_formula else max(float(estimated_time) - float(max_time), 0.0)
    return _clamp01((overtime / _positive(hide_time, eps)) * float(tau))


def safe_score(
    estimated_time: float,
    max_time: float,
    hide_time: float,
    tau: float = 1.0,
    doc_formula: bool = False,
    eps: float = 1e-6,
) -> float:
    return _clamp01(1.0 - exposure_probability(estimated_time, max_time, hide_time, tau, doc_formula, eps))


# 公式(18)：备选路线加权 ETA。
# 把多条备选路线的 ETA 按权重合成一个冗余时间。
# 用于衡量候选路线集合是否有足够备用空间。
def weighted_redundant_time(estimated_times: Sequence[float], weights: Sequence[float]) -> float:
    return sum(float(t) * float(w) for t, w in zip(estimated_times, weights))


# 公式(19)：路线冗余评分。
# 用备选路线加权 ETA 与最大可用时间比较，得到冗余能力评分。
# 默认同样采用“冗余时间越小越好”的工程修正版。
def redundancy_score(
    estimated_times: Sequence[float],
    weights: Sequence[float],
    max_time: float,
    doc_formula: bool = False,
    eps: float = 1e-6,
) -> float:
    red_time = weighted_redundant_time(estimated_times, weights)
    numerator = red_time if doc_formula else max_time
    denominator = max_time if doc_formula else red_time
    return math.log(_positive(numerator, eps) / _positive(denominator, eps))


# 公式(20)：Sigmoid 归一化。
# 把任意实数评分压到 0~1，便于不同公式结果进入统一加权。
# 做了指数溢出保护，避免极端输入破坏流程。
def sigmoid_normalize(score: float, k: float = 1.0, s0: float = 0.0) -> float:
    x = -float(k) * (float(score) - float(s0))
    if x > 700.0:
        return 0.0
    if x < -700.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(x))


# 公式(21)：发射平台综合评分。
# 将时间、冗余和安全三个归一化评分合成为发射平台总分。
# 权重可以按任务书或现场经验调整。
def launcher_total_score(
    time_score_norm: float,
    red_score_norm: float,
    safe_score_norm: float,
    delta_time: float = 0.4,
    delta_red: float = 0.2,
    delta_safe: float = 0.4,
) -> float:
    return (
        float(delta_time) * float(time_score_norm)
        + float(delta_red) * float(red_score_norm)
        + float(delta_safe) * float(safe_score_norm)
    )


# 公式(22)：贮备库繁忙程度惩罚。
# 用已选择数量与容量上限的比例描述库的繁忙程度。
# 当前实现只有超过容量才惩罚，避免在容量内过早干扰分配。
def depot_busy_score(selected_count: int, capacity: int, alpha: float = 1.0, eps: float = 1e-6) -> float:
    return -float(alpha) * max(0.0, (float(selected_count) - float(capacity)) / _positive(float(capacity), eps))


# 公式(23)：贮备库等待/准备时间惩罚。
# 将补给等待或准备时间归一化为负向惩罚。
# 等待越久，贮备库综合分越低。
def depot_prep_score(prep_time: float, max_time: float, beta: float = 1.0, eps: float = 1e-6) -> float:
    return -float(beta) * float(prep_time) / _positive(max_time, eps)


# 公式(24)：最近 top_k 个平台到达贮备库的平均 ETA。
# 取最快到达的若干车辆平均时间，表示该库对当前任务群的区位优势。
# 没有候选车辆时返回 0，保证流程可降级运行。
def depot_average_arrival_time(arrival_times: Sequence[float], top_k: int = 3) -> float:
    values = sorted(float(t) for t in arrival_times)
    if not values:
        return 0.0
    selected = values[: max(1, int(top_k))]
    return sum(selected) / len(selected)


# 公式(25)：贮备库区位时间评分。
# 用最近车辆平均到达时间与最大时间窗口比较，形成时间评分。
# 该分数后续会归一化后进入贮备库综合分。
def depot_time_score(
    arrival_times: Sequence[float],
    max_time: float,
    top_k: int = 3,
    doc_formula: bool = False,
    eps: float = 1e-6,
) -> float:
    average = depot_average_arrival_time(arrival_times, top_k)
    numerator = average if doc_formula else max_time
    denominator = max_time if doc_formula else average
    return math.log(_positive(numerator, eps) / _positive(denominator, eps))


# 公式(26)：贮备库综合评分。
# 汇总繁忙、准备等待和区位时间三个维度。
# 这是贮备库任务书评分的总出口，便于日志中追溯。
def depot_total_score(busy_score: float, prep_score: float, time_score_norm: float) -> float:
    return float(busy_score) + float(prep_score) + float(time_score_norm)
