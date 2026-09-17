from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


def score_depot_vehicle_rows(
    distances: Iterable[Tuple[str, float]],
    *,
    distance_weight: float,
    rank_weight: float,
) -> List[Dict[str, Any]]:
    """One depot scores all selected vehicles: a*distance + b*distance_rank."""
    ordered = sorted(
        ((str(vehicle_id), max(0.0, float(distance_m))) for vehicle_id, distance_m in distances),
        key=lambda item: (item[1], item[0]),
    )
    rows: List[Dict[str, Any]] = []
    for rank, (vehicle_id, distance_m) in enumerate(ordered, start=1):
        rank_score = float(rank)
        rows.append(
            {
                "vehicle_id": vehicle_id,
                "distance_m": round(distance_m, 3),
                "distance_rank": rank,
                "distance_rank_score": rank_score,
                "score_total": round(float(distance_weight) * distance_m + float(rank_weight) * rank_score, 6),
                "score_semantics": "task_book_depot_distance_rank_cost",
            }
        )
    return rows
