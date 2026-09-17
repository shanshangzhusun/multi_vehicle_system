from __future__ import annotations

import math

from mvs.common.formula_utils import (
    GridFeature,
    depot_busy_score,
    estimate_eta_by_grid_model,
    grid_features_from_polyline,
    has_segment_capacity_conflict,
    launcher_total_score,
    sigmoid_normalize,
    time_window_overlap,
)


class ConstantVelocityModel:
    def __init__(self, speed_mps: float) -> None:
        self.speed_mps = speed_mps

    def predict(self, features):
        return self.speed_mps


def test_time_window_overlap() -> None:
    assert time_window_overlap(10.0, 20.0, 15.0, 25.0) == 5.0
    assert time_window_overlap(10.0, 20.0, 20.0, 30.0) == 0.0


def test_capacity_conflict() -> None:
    assert has_segment_capacity_conflict(3, 2)
    assert not has_segment_capacity_conflict(2, 2)


def test_eta_constant_velocity_model() -> None:
    grids = [GridFeature(length_m=10.0), GridFeature(length_m=20.0)]
    eta = estimate_eta_by_grid_model(grids, ConstantVelocityModel(5.0), initial_speed_mps=5.0)
    assert math.isclose(eta, 6.0)


def test_grid_features_from_polyline_equal_distance() -> None:
    grids = grid_features_from_polyline([(0.0, 0.0), (25.0, 0.0)], grid_length_m=10.0)
    assert [round(g.length_m, 3) for g in grids] == [10.0, 10.0, 5.0]
    assert all(math.isclose(g.heading_in_rad, 0.0) for g in grids)


def test_sigmoid_zero() -> None:
    assert sigmoid_normalize(0.0, k=1.0, s0=0.0) == 0.5


def test_launcher_total_score() -> None:
    assert math.isclose(launcher_total_score(0.5, 0.25, 1.0), 0.65)


def test_depot_busy_score() -> None:
    assert depot_busy_score(1, 2) == 0.0
    assert depot_busy_score(3, 2) < 0.0
