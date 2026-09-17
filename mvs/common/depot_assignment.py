from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from mvs.common.depot_scoring import score_depot_vehicle_rows

#贮备库打分
def score_depot_vehicles(
    distances: Iterable[Tuple[str, float]],
    *,
    distance_weight: float,
    rank_weight: float,
) -> List[Dict[str, Any]]:
    """Score vehicles for one depot; a longer route always has a larger rank."""
    # 单个贮备库给所有已选车辆打分：分数 = a*发射点到本库距离 + b*距离排名。
    # 真实计算步骤放在 mvs.common.depot_scoring，主流程只保留兼容包装。
    return score_depot_vehicle_rows(distances, distance_weight=distance_weight, rank_weight=rank_weight)


def greedy_capacity_assignment(
    depot_rows: Iterable[Dict[str, Any]],
    vehicle_ids: Iterable[str],
    *,
    load_ratio_weight: float = 0.0,
) -> Dict[str, Any]:
    """Apply the requested global-minimum, delete-column, consume-capacity rule."""
    # 全局分配矩阵：每行是一个贮备库，每列是一辆已选车。
    # 每轮取当前全局最小分，删除该车辆列并消耗库容量；库满后自然不再参与后续分配。
    remaining_vehicles = {str(vehicle_id) for vehicle_id in vehicle_ids}
    depots: Dict[str, Dict[str, Any]] = {}
    for raw in depot_rows:
        depot_id = str(raw.get("depot_id") or "")
        if not depot_id:
            continue
        capacity = max(0, int(raw.get("capacity", 0) or 0))
        scores = {
            str(row.get("vehicle_id")): dict(row)
            for row in (raw.get("scores") or [])
            if isinstance(row, dict) and row.get("vehicle_id") not in {None, ""}
        }
        depots[depot_id] = {
            **dict(raw),
            "capacity": capacity,
            "original_capacity": capacity,
            "scores_by_vehicle": scores,
        }

    assignments: List[Dict[str, Any]] = []
    while remaining_vehicles:
        candidates: List[Tuple[float, str, str, Dict[str, Any]]] = []
        for depot_id, depot in depots.items():
            if int(depot["capacity"]) <= 0:
                continue
            # 动态负载惩罚体现“已经用了多少容量”。容量内不是硬禁止，
            # 只是随着 used/original_capacity 增加，让后续车辆更愿意流向空闲库。
            used = int(depot["original_capacity"]) - int(depot["capacity"])
            load_ratio = used / max(1, int(depot["original_capacity"]))
            load_penalty = max(0.0, float(load_ratio_weight)) * load_ratio
            for vehicle_id in remaining_vehicles:
                score = depot["scores_by_vehicle"].get(vehicle_id)
                if score is None:
                    continue
                # 贮备库全局分配实际计算点：基础矩阵分 + c*当前负载率。
                # 每轮取 dynamic_total 最小项，删掉该车辆列并扣减对应库容量。
                dynamic_total = float(score.get("score_total", float("inf"))) + load_penalty
                candidates.append(
                    (dynamic_total, depot_id, vehicle_id, {**score, "load_ratio": load_ratio, "load_penalty": load_penalty})
                )
        if not candidates:
            break
        total, depot_id, vehicle_id, score = min(candidates, key=lambda row: (row[0], row[1], row[2]))
        depot = depots[depot_id]
        assignments.append(
            {
                "vehicle_id": vehicle_id,
                "depot_id": depot_id,
                "score_total": total,
                "base_score_total": score.get("score_total"),
                "load_ratio_before_assignment": round(float(score.get("load_ratio", 0.0)), 6),
                "load_penalty": round(float(score.get("load_penalty", 0.0)), 6),
                "distance_m": score.get("distance_m"),
                "distance_rank": score.get("distance_rank"),
                "distance_rank_score": score.get("distance_rank_score"),
                "depot_node": depot.get("depot_node"),
                "depot_port": depot.get("depot_port"),
                "capacity": int(raw_capacity(depot)),
            }
        )
        remaining_vehicles.remove(vehicle_id)
        depot["capacity"] = int(depot["capacity"]) - 1

    return {
        "assignments": assignments,
        "unassigned_vehicle_ids": sorted(remaining_vehicles),
        "assigned_count": len(assignments),
    }


def raw_capacity(depot: Dict[str, Any]) -> int:
    value = depot.get("original_capacity")
    if value is None:
        value = int(depot.get("capacity", 0) or 0)
    return max(0, int(value))
