from __future__ import annotations

from mvs.common.models import make_envelope
from mvs.common.platform_interfaces import (
    MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
    MSG_DEPOT_VEHICLE_SCORE_RESULT,
    MSG_ZHU_BEI_CONTEXT,
)
from mvs.common.scoring import DepotState
from mvs.depot.depot_app import DepotApp


def test_zhu_bei_context_response_uses_msg_ip_and_depot_id() -> None:
    app = DepotApp.__new__(DepotApp)
    app.depot_model_cfg = {"host": "192.168.2.10", "port": 8888}
    env = make_envelope(
        MSG_ZHU_BEI_CONTEXT,
        "model",
        "depot_8601",
        {
            "msg_ip": "192.168.2.8",
            "reply_port": 8601,
            "depot_id": "8602",
            "msg_type": "REQUEST_DEPOT_SCORE",
        },
    )

    assert app._response_address(env, ("192.168.2.10", 50000)) == ("192.168.2.8", 8602)


def test_normal_depot_request_keeps_protocol_reply_address() -> None:
    app = DepotApp.__new__(DepotApp)
    app.depot_model_cfg = {"host": "192.168.2.10", "port": 8888}
    env = make_envelope(
        "REQUEST_DEPOT_SCORE",
        "caller",
        "depot_8601",
        {"msg_ip": "192.168.2.8", "reply_port": 9191},
    )

    assert app._response_address(env, ("127.0.0.1", 50000)) == ("192.168.2.8", 9191)


def test_depot_context_without_launch_returns_readiness_score() -> None:
    app = DepotApp.__new__(DepotApp)
    app.depot_id = "8601"
    app.default_capacity = 1
    app.reload_duration_sec = 60.0
    app.points = type("Points", (), {"depots": ["depot_front_1"]})()
    app.depot_states = {
        "depot_front_1": DepotState(
            depot_id="depot_front_1",
            capacity=1,
            queue_count=0,
            occupied=False,
            wait_prep_sec=0.0,
        )
    }

    result = app._score_depot_readiness({"depot_id": "8601", "vehicle_id": "8601"})

    assert result["selected_depot"] == "8601"
    assert result["depot_scores"][0]["score_total"] == 100.0
    assert result["depot_scores"][0]["score_semantics"] == "depot_self_readiness_without_task_route"


def test_vehicle_score_context_uses_euclidean_distance_without_shortest_path() -> None:
    class Node:
        def __init__(self, x: float, y: float) -> None:
            self.x = x
            self.y = y

    class Graph:
        nodes = {"launch": Node(0.0, 0.0), "depot": Node(3000.0, 4000.0)}

        def shortest_path(self, *_args):
            raise AssertionError("euclidean scoring must not run road-network search")

    class Log:
        def log(self, *_args, **_kwargs) -> None:
            pass

    app = DepotApp.__new__(DepotApp)
    app.depot_id = "8601"
    app.node_id = "depot_8601"
    app.listen_port = 8601
    app.default_capacity = 16
    app.points = type("Points", (), {"depots": ["depot"]})()
    app.graph = Graph()
    app.event_log = Log()
    app._resolve_runtime_node = lambda *values: next(
        (str(value) for value in values if value in {"launch", "depot"}), None
    )
    sent = []
    app._send_raw_json = lambda msg_type, payload, addr: sent.append((msg_type, payload, addr))
    env = make_envelope(
        MSG_DEPOT_VEHICLE_SCORE_CONTEXT,
        "scheduler_001",
        "depot_8601",
        {
            "task_id": "task_1",
            "depot_id": "8601",
            "depot_node": "depot",
            "depot_port": 8601,
            "capacity": 16,
            "reply_host": "192.168.2.8",
            "reply_port": 9120,
            "score_weights": {
                "distance_metric": "euclidean",
                "distance_weight_a": 1.0,
                "distance_rank_weight_b": 100.0,
            },
            "assignments": [{"vehicle_id": "8414", "launch_node": "launch"}],
        },
    )

    app._on_vehicle_score_context(env, ("192.168.2.8", 9120))

    assert sent[0][0] == MSG_DEPOT_VEHICLE_SCORE_RESULT
    assert sent[0][1]["distance_metric"] == "euclidean"
    assert sent[0][1]["scores"][0]["distance_m"] == 5000.0
    assert sent[0][1]["scores"][0]["score_total"] == 5100.0
