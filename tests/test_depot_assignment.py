from mvs.common.depot_assignment import greedy_capacity_assignment, score_depot_vehicles


def test_depot_score_uses_distance_and_ascending_distance_rank() -> None:
    rows = score_depot_vehicles(
        [("v_far", 3000.0), ("v_near", 1000.0), ("v_mid", 2000.0)],
        distance_weight=1.0,
        rank_weight=100.0,
    )

    assert [row["vehicle_id"] for row in rows] == ["v_near", "v_mid", "v_far"]
    assert [row["distance_rank"] for row in rows] == [1, 2, 3]
    assert [row["score_total"] for row in rows] == [1100.0, 2200.0, 3300.0]


def test_global_minimum_assignment_consumes_vehicle_columns_and_capacity_rows() -> None:
    result = greedy_capacity_assignment(
        [
            {
                "depot_id": "d1",
                "depot_port": 8601,
                "capacity": 1,
                "scores": [
                    {"vehicle_id": "v1", "score_total": 10.0},
                    {"vehicle_id": "v2", "score_total": 11.0},
                ],
            },
            {
                "depot_id": "d2",
                "depot_port": 8602,
                "capacity": 1,
                "scores": [
                    {"vehicle_id": "v1", "score_total": 12.0},
                    {"vehicle_id": "v2", "score_total": 20.0},
                ],
            },
        ],
        ["v1", "v2"],
    )

    assert [(row["vehicle_id"], row["depot_id"]) for row in result["assignments"]] == [
        ("v1", "d1"),
        ("v2", "d2"),
    ]
    assert result["unassigned_vehicle_ids"] == []


def test_global_assignment_reports_capacity_shortage() -> None:
    result = greedy_capacity_assignment(
        [
            {
                "depot_id": "d1",
                "capacity": 1,
                "scores": [
                    {"vehicle_id": "v1", "score_total": 1.0},
                    {"vehicle_id": "v2", "score_total": 2.0},
                ],
            }
        ],
        ["v1", "v2"],
    )

    assert result["assigned_count"] == 1
    assert result["unassigned_vehicle_ids"] == ["v2"]


def test_dynamic_load_penalty_prefers_less_loaded_depot_for_close_scores() -> None:
    scores = [
        {"vehicle_id": "v1", "score_total": 1000.0},
        {"vehicle_id": "v2", "score_total": 1000.0},
    ]
    result = greedy_capacity_assignment(
        [
            {"depot_id": "d1", "capacity": 16, "scores": scores},
            {"depot_id": "d2", "capacity": 16, "scores": scores},
        ],
        ["v1", "v2"],
        load_ratio_weight=1000.0,
    )

    assert [row["depot_id"] for row in result["assignments"]] == ["d1", "d2"]
    assert result["assignments"][1]["load_ratio_before_assignment"] == 0.0
