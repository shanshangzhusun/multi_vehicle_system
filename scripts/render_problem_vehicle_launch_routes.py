#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection


VEHICLE_LAUNCHES = {
    "8564": "发射阵地_170",
    "8565": "发射阵地_169",
    "8566": "发射阵地_168",
}

COLORS = {
    "8564": "#d73027",
    "8565": "#1f78b4",
    "8566": "#33a02c",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    radius = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    value = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return radius * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))


def path_length_m(points: list[tuple[float, float]]) -> float:
    return sum(haversine_m(points[idx - 1], points[idx]) for idx in range(1, len(points)))


def latest_candidate_capture(result_dir: Path, vehicle_id: str) -> Path:
    captures = sorted(
        (result_dir / "message_capture" / "vehicle" / vehicle_id / "send").glob(
            "*_VEHICLE_CANDIDATE_PATH_RESULT.json"
        )
    )
    if not captures:
        raise FileNotFoundError(f"No candidate path capture found for vehicle {vehicle_id}")
    return captures[-1]


def load_rank1_route(result_dir: Path, vehicle_id: str) -> dict[str, Any]:
    capture = latest_candidate_capture(result_dir, vehicle_id)
    payload = load_json(capture).get("payload") or {}
    candidates = [item for item in payload.get("paths", []) if int(item.get("rank", -1)) == 1]
    if not candidates:
        raise ValueError(f"Vehicle {vehicle_id} has no rank-1 route in {capture}")
    route = candidates[0]
    points = [
        (float(point["lon"]), float(point["lat"]))
        for point in route.get("path_points", [])
        if point.get("lon") is not None and point.get("lat") is not None
    ]
    if len(points) < 2:
        raise ValueError(f"Vehicle {vehicle_id} rank-1 route has fewer than two points")
    return {
        "vehicle_id": vehicle_id,
        "launch_node": str(route.get("launch_node") or ""),
        "declared_distance_m": float(route.get("route_distance_m") or 0.0),
        "travel_seconds": float(route.get("travel_seconds") or 0.0),
        "points": points,
        "geometry_distance_m": path_length_m(points),
        "capture": str(capture),
    }


def load_launch_points(capture: Path) -> dict[str, tuple[float, float]]:
    payload = load_json(capture).get("payload") or {}
    rows = payload.get("data") or []
    return {
        str(row["name"]): (float(row["lon"]), float(row["lat"]))
        for row in rows
        if isinstance(row, dict) and row.get("name") and row.get("lon") is not None and row.get("lat") is not None
    }


def load_road_lines(graph_path: Path) -> list[list[tuple[float, float]]]:
    graph = load_json(graph_path)
    lines: list[list[tuple[float, float]]] = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.get("edges", []):
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        key = tuple(sorted((src, dst)))
        if key in seen:
            continue
        seen.add(key)
        geometry = edge.get("geometry") or []
        line = [
            (float(point["lon"]), float(point["lat"]))
            for point in geometry
            if point.get("lon") is not None and point.get("lat") is not None
        ]
        if len(line) >= 2:
            lines.append(line)
    return lines


def padded_bounds(points: list[tuple[float, float]], pad_ratio: float = 0.08) -> tuple[float, float, float, float]:
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    lon_span = max(max(lons) - min(lons), 0.01)
    lat_span = max(max(lats) - min(lats), 0.01)
    return (
        min(lons) - lon_span * pad_ratio,
        max(lons) + lon_span * pad_ratio,
        min(lats) - lat_span * pad_ratio,
        max(lats) + lat_span * pad_ratio,
    )


def visible_road_lines(
    road_lines: list[list[tuple[float, float]]], bounds: tuple[float, float, float, float]
) -> list[list[tuple[float, float]]]:
    min_lon, max_lon, min_lat, max_lat = bounds
    visible = []
    for line in road_lines:
        lons = [point[0] for point in line]
        lats = [point[1] for point in line]
        if max(lons) < min_lon or min(lons) > max_lon or max(lats) < min_lat or min(lats) > max_lat:
            continue
        visible.append(line)
    return visible


def setup_axis(ax: Any, bounds: tuple[float, float, float, float], road_lines: list[list[tuple[float, float]]]) -> None:
    min_lon, max_lon, min_lat, max_lat = bounds
    visible = visible_road_lines(road_lines, bounds)
    if visible:
        ax.add_collection(LineCollection(visible, colors="#8c8c8c", linewidths=0.48, alpha=0.72, zorder=1))
    ax.set_xlim(min_lon, max_lon)
    ax.set_ylim(min_lat, max_lat)
    mean_lat = (min_lat + max_lat) / 2.0
    ax.set_aspect(1.0 / math.cos(math.radians(mean_lat)), adjustable="box")
    ax.grid(True, color="#d9d9d9", linewidth=0.35, alpha=0.55)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")


def draw_route(
    ax: Any,
    route: dict[str, Any],
    launch: tuple[float, float],
    color: str,
    show_stats: bool = True,
) -> None:
    vehicle_id = route["vehicle_id"]
    points = route["points"]
    start = points[0]
    endpoint = points[-1]
    ax.plot(
        [point[0] for point in points],
        [point[1] for point in points],
        color=color,
        linewidth=2.25,
        alpha=0.96,
        zorder=4,
        label=f"Vehicle {vehicle_id} returned route",
    )
    ax.plot(
        [start[0], launch[0]],
        [start[1], launch[1]],
        color=color,
        linewidth=1.1,
        linestyle="--",
        alpha=0.85,
        zorder=3,
        label=f"Vehicle {vehicle_id} straight line",
    )
    ax.scatter([start[0]], [start[1]], marker="s", s=75, color=color, edgecolor="white", linewidth=0.8, zorder=7)
    ax.scatter([launch[0]], [launch[1]], marker="*", s=175, color="#ffbf00", edgecolor="#222222", linewidth=0.8, zorder=8)
    ax.scatter([endpoint[0]], [endpoint[1]], marker="x", s=70, color="#111111", linewidth=1.3, zorder=9)
    ax.annotate(f"V{vehicle_id}", start, xytext=(5, 5), textcoords="offset points", fontsize=8, zorder=10)
    ax.annotate(f"Launch {VEHICLE_LAUNCHES[vehicle_id].split('_')[-1]}", launch, xytext=(5, 5), textcoords="offset points", fontsize=8, zorder=10)
    if show_stats:
        straight = haversine_m(start, launch)
        endpoint_offset = haversine_m(endpoint, launch)
        stats = (
            f"straight: {straight / 1000.0:.2f} km\n"
            f"returned geometry: {route['geometry_distance_m'] / 1000.0:.2f} km\n"
            f"reported distance: {route['declared_distance_m'] / 1000.0:.2f} km\n"
            f"endpoint offset: {endpoint_offset:.2f} m\n"
            f"travel: {route['travel_seconds']:.1f} s"
        )
        ax.text(
            0.015,
            0.985,
            stats,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=8.5,
            bbox={"facecolor": "white", "edgecolor": "#777777", "alpha": 0.9, "boxstyle": "round,pad=0.35"},
            zorder=12,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Render returned routes for vehicles 8564-8566 over the runtime road graph.")
    parser.add_argument("--result-dir", default="result")
    parser.add_argument("--scheduler-id", default="scheduler_002")
    parser.add_argument("--road-graph", default="result/theaters/guangdong/data/road_graph_shp_demo.json")
    parser.add_argument("--out-dir", default="result/visuals/problem_routes_8564_8566")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    launch_capture = result_dir / "message_capture" / "scheduler" / args.scheduler_id / "recv" / "000003_FA_SHE_DIAN.json"
    if not launch_capture.exists():
        matches = sorted(launch_capture.parent.glob("*_FA_SHE_DIAN.json"))
        if not matches:
            raise FileNotFoundError(f"No FA_SHE_DIAN capture under {launch_capture.parent}")
        launch_capture = matches[-1]

    launch_points = load_launch_points(launch_capture)
    road_lines = load_road_lines(Path(args.road_graph))
    routes = [load_rank1_route(result_dir, vehicle_id) for vehicle_id in VEHICLE_LAUNCHES]
    summaries = []

    for route in routes:
        vehicle_id = route["vehicle_id"]
        launch_name = VEHICLE_LAUNCHES[vehicle_id]
        launch = launch_points[launch_name]
        route["launch_name"] = launch_name
        route["launch_point"] = launch
        bounds = padded_bounds(route["points"] + [launch], pad_ratio=0.09)
        fig, ax = plt.subplots(figsize=(10, 8), dpi=180)
        setup_axis(ax, bounds, road_lines)
        draw_route(ax, route, launch, COLORS[vehicle_id])
        ax.set_title(f"Vehicle {vehicle_id} to Launch {launch_name.split('_')[-1]}: returned route on runtime road network")
        ax.legend(loc="lower right", fontsize=7.5, framealpha=0.92)
        fig.tight_layout()
        fig.savefig(out_dir / f"vehicle_{vehicle_id}_launch_{launch_name.split('_')[-1]}.png")
        plt.close(fig)

        start = route["points"][0]
        endpoint = route["points"][-1]
        summaries.append(
            {
                "vehicle_id": vehicle_id,
                "launch_name": launch_name,
                "vehicle_start": {"lon": start[0], "lat": start[1]},
                "original_launch": {"lon": launch[0], "lat": launch[1]},
                "returned_endpoint": {"lon": endpoint[0], "lat": endpoint[1]},
                "straight_distance_m": round(haversine_m(start, launch), 3),
                "returned_geometry_distance_m": round(route["geometry_distance_m"], 3),
                "reported_route_distance_m": route["declared_distance_m"],
                "endpoint_offset_m": round(haversine_m(endpoint, launch), 3),
                "travel_seconds": route["travel_seconds"],
                "capture": route["capture"],
            }
        )

    all_points = [point for route in routes for point in route["points"]]
    all_points.extend(route["launch_point"] for route in routes)
    overview_bounds = padded_bounds(all_points, pad_ratio=0.07)
    fig, ax = plt.subplots(figsize=(11, 9), dpi=180)
    setup_axis(ax, overview_bounds, road_lines)
    for route in routes:
        draw_route(ax, route, route["launch_point"], COLORS[route["vehicle_id"]], show_stats=False)
    ax.set_title("Vehicles 8564-8566: returned rank-1 routes and runtime road network")
    ax.legend(loc="best", fontsize=7.2, framealpha=0.92, ncol=2)
    fig.tight_layout()
    fig.savefig(out_dir / "overview_8564_8566.png")
    plt.close(fig)

    (out_dir / "route_summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "routes": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
