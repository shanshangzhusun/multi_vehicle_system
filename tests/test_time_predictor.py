from __future__ import annotations

import json
from types import SimpleNamespace

from mvs.vehicle.time_predictor import MLPTimePredictor
from mvs.vehicle.vehicle_app import VehicleApp


def test_json_mlp_predictor_uses_network(tmp_path):
    model = {
        "feature_names": ["distance_m", "speed_mps", "phase_to_fire"],
        "input_mean": [0.0, 0.0, 0.0],
        "input_std": [1.0, 1.0, 1.0],
        "layers": [
            {
                "weights": [[0.0, 0.0, 0.0]],
                "bias": [1.2],
                "activation": "linear",
            }
        ],
    }
    model_path = tmp_path / "eta.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")

    predictor = MLPTimePredictor(str(model_path), fallback_bias=1.05)
    seconds = predictor.predict_with_context(
        {
            "distance_m": 100.0,
            "speed_mps": 10.0,
            "phase": "to_fire",
        }
    )

    assert predictor.network_enabled is True
    assert predictor.predict_count == 1
    assert predictor.network_predict_count == 1
    assert seconds == 12.0


def test_exit_speed_mlp_predictor_uses_six_feature_network(tmp_path):
    model = {
        "output_type": "exit_speed_mps",
        "feature_names": [
            "length_m",
            "curvature",
            "grade",
            "allowed_speed_mps",
            "enter_speed_mps",
            "max_accel_mps2",
        ],
        "input_mean": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "input_std": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        "layers": [
            {
                "weights": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
                "bias": [10.0],
                "activation": "linear",
            }
        ],
    }
    model_path = tmp_path / "eta_exit_speed.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")

    predictor = MLPTimePredictor(str(model_path), fallback_bias=1.05)
    seconds = predictor.predict_with_context(
        {
            "length_m": 100.0,
            "distance_m": 100.0,
            "curvature": 0.0,
            "grade": 0.0,
            "allowed_speed_mps": 10.0,
            "enter_speed_mps": 10.0,
            "speed_mps": 10.0,
            "max_accel_mps2": 1.0,
        }
    )

    assert predictor.network_enabled is True
    assert predictor.network_predict_count == 1
    assert seconds == 10.0


def test_vehicle_segment_time_ignores_road_speed_limit():
    app = VehicleApp.__new__(VehicleApp)
    app.speed_mps = 22.222
    app.kinematics = SimpleNamespace(max_speed_mps=22.222)
    app.max_accel_mps2 = 1.0
    app.time_predictor = MLPTimePredictor(fallback_bias=1.0)

    seconds = app._travel_time_seconds(
        path_cost=2222.2,
        phase="to_fire",
        speed_limit_mps=8.0,
    )

    assert round(seconds, 6) == 100.0
