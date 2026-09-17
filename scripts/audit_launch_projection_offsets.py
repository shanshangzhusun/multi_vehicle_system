#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import matplotlib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

from mvs.scheduler.map_model import MapLoader, RoadGraph


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_capture(capture_root: Path, scheduler_id: str, msg_type: str) -> Optional[Path]:
    recv_dir = capture_root / "scheduler" / scheduler_id / "recv"
    paths = sorted(recv_dir.glob(f"*_{msg_type}.json"))
    return paths[-1] if paths else None


def _normalize_dian_rows(payload: dict) -> List[dict]:
    data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in (
            "points",
            "items",
            "vehicles",
            "fa_she_dian",
            "yin_bi_dian",
            "vehicle_dian",
            "depot_dian",
            "zhu_bei_dian",
            "zhu_bei_ku_dian",
            "depots",
        ):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = [data]
    else:
        rows = []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _dian_row_key(row: dict, fallback: int) -> str:
    for key in ("id", "point_id", "vehicle_id", "port", "name", "index"):
        value = row.get(key)
        if value not in {None, ""}:
            return str(value)
    return f"idx:{fallback}"


def _dian_graph_point_id(row: dict, prefix: str) -> str:
    if prefix in {"launch", "hide", "depot"}:
        raw = ""
        for key in ("id", "point_id", "fire_point_id", "launch_node", "node_id", "name", "index"):
            value = row.get(key)
            if value not in {None, ""}:
                raw = str(value)
                break
        if not raw:
            raw = _dian_row_key(row, 0)
    else:
        raw = _dian_row_key(row, 0)
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(raw))
    return f"{prefix}_{safe}"


def _graph_xy_looks_lonlat(graph: RoadGraph) -> bool:
    if not graph.nodes:
        return False
    xs = [node.x for node in graph.nodes.values()]
    ys = [node.y for node in graph.nodes.values()]
    return min(xs) >= -180.0 and max(xs) <= 180.0 and min(ys) >= -90.0 and max(ys) <= 90.0


def _build_lonlat_xy_transform(graph: RoadGraph) -> Optional[Tuple[float, float, float, float]]:
    samples: List[Tuple[float, float, float, float]] = []
    for node in graph.nodes.values():
        if node.lon is None or node.lat is None:
            continue
        samples.append((float(node.lon), float(node.lat), float(node.x), float(node.y)))
    if len(samples) < 2:
        return None

    def fit(src_vals: List[float], dst_vals: List[float]) -> Optional[Tuple[float, float]]:
        src_mean = sum(src_vals) / len(src_vals)
        dst_mean = sum(dst_vals) / len(dst_vals)
        var = sum((v - src_mean) ** 2 for v in src_vals)
        if var <= 1e-12:
            return None
        cov = sum((s - src_mean) * (d - dst_mean) for s, d in zip(src_vals, dst_vals))
        scale = cov / var
        bias = dst_mean - scale * src_mean
        return scale, bias

    x_fit = fit([s[0] for s in samples], [s[2] for s in samples])
    y_fit = fit([s[1] for s in samples], [s[3] for s in samples])
    if x_fit is None or y_fit is None:
        return None
    return x_fit[0], x_fit[1], y_fit[0], y_fit[1]


def _graph_xy_to_lonlat(transform: Optional[Tuple[float, float, float, float]], x: float, y: float) -> Optional[Tuple[float, float]]:
    if not transform:
        return None
    x_scale, x_bias, y_scale, y_bias = transform
    if abs(x_scale) <= 1e-12 or abs(y_scale) <= 1e-12:
        return None
    return (x - x_bias) / x_scale, (y - y_bias) / y_scale


def _dian_xy(graph: RoadGraph, transform: Optional[Tuple[float, float, float, float]], row: dict) -> Optional[Tuple[float, float]]:
    lon = row.get("lon", row.get("lng", row.get("longitude", row.get("platform_LocationLLA_Lon"))))
    lat = row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat")))
    if lon is not None and lat is not None:
        lon_f = float(lon)
        lat_f = float(lat)
        if _graph_xy_looks_lonlat(graph):
            return lon_f, lat_f
        if transform is not None:
            x_scale, x_bias, y_scale, y_bias = transform
            return x_scale * lon_f + x_bias, y_scale * lat_f + y_bias
    x = row.get("x")
    y = row.get("y")
    if x is not None and y is not None:
        return float(x), float(y)
    return None


def _point_on_edge_geometry(graph: RoadGraph, edge: Any, ratio: float) -> Optional[Tuple[float, float]]:
    poly = list(edge.geometry or [])
    if len(poly) < 2:
        a = graph.nodes.get(edge.src)
        b = graph.nodes.get(edge.dst)
        if not a or not b:
            return None
        poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
    lengths = [
        math.hypot(float(poly[i + 1]["x"]) - float(poly[i]["x"]), float(poly[i + 1]["y"]) - float(poly[i]["y"]))
        for i in range(len(poly) - 1)
    ]
    total = sum(lengths)
    if total <= 1e-9:
        return float(poly[0]["x"]), float(poly[0]["y"])
    target = min(1.0, max(0.0, float(ratio))) * total
    traversed = 0.0
    for idx, length in enumerate(lengths):
        if length <= 1e-9:
            continue
        if traversed + length >= target:
            local = (target - traversed) / length
            ax, ay = float(poly[idx]["x"]), float(poly[idx]["y"])
            bx, by = float(poly[idx + 1]["x"]), float(poly[idx + 1]["y"])
            return ax + (bx - ax) * local, ay + (by - ay) * local
        traversed += length
    return float(poly[-1]["x"]), float(poly[-1]["y"])


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _project_launch_rows(graph: RoadGraph, rows: List[dict]) -> List[dict]:
    transform = _build_lonlat_xy_transform(graph)
    out: List[dict] = []
    for idx, row in enumerate(rows):
        if row.get("lon") is None or row.get("lat") is None:
            continue
        lon = float(row["lon"])
        lat = float(row["lat"])
        xy = _dian_xy(graph, transform, row)
        if xy is None:
            continue
        nearest_edge = MapLoader._nearest_edge_projection(graph, xy[0], xy[1])
        if nearest_edge is None:
            continue
        edge, ratio = nearest_edge
        projected_xy = _point_on_edge_geometry(graph, edge, ratio)
        projected_x, projected_y = projected_xy if projected_xy is not None else xy
        oriented_ratio = float(ratio)
        if edge.geometry and not (edge.geom_from == edge.src and edge.geom_to == edge.dst):
            oriented_ratio = 1.0 - oriented_ratio
        point_id = _dian_graph_point_id(row, "launch")
        if point_id not in graph.nodes:
            graph.add_point_on_edge(
                point_id=point_id,
                from_node=edge.src,
                to_node=edge.dst,
                ratio=oriented_ratio,
                x=projected_x,
                y=projected_y,
            )
        node = graph.nodes[point_id]
        projected_lonlat = _graph_xy_to_lonlat(transform, projected_x, projected_y)
        if projected_lonlat is not None:
            node.lon, node.lat = projected_lonlat
        if node.lon is None or node.lat is None:
            continue
        projected_lon = float(node.lon)
        projected_lat = float(node.lat)
        out.append(
            {
                "row_index": idx,
                "point_id": point_id,
                "name": row.get("name") or "",
                "index": row.get("index") or "",
                "raw_lon": lon,
                "raw_lat": lat,
                "raw_x": xy[0],
                "raw_y": xy[1],
                "projected_lon": projected_lon,
                "projected_lat": projected_lat,
                "projected_x": projected_x,
                "projected_y": projected_y,
                "offset_m": _haversine_m(lon, lat, projected_lon, projected_lat),
                "edge_src": edge.src,
                "edge_dst": edge.dst,
                "edge_ratio": round(float(ratio), 6),
            }
        )
    return out


def _edge_xy_lines(graph: RoadGraph) -> List[Tuple[float, float, float, float]]:
    lines: List[Tuple[float, float, float, float]] = []
    seen = set()
    for meta in graph.edge_meta.values():
        key = (meta.src, meta.dst)
        if key in seen:
            continue
        seen.add(key)
        poly = list(meta.geometry or [])
        if len(poly) < 2:
            a = graph.nodes.get(meta.src)
            b = graph.nodes.get(meta.dst)
            if not a or not b:
                continue
            poly = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
        for idx in range(len(poly) - 1):
            lines.append(
                (
                    float(poly[idx]["x"]),
                    float(poly[idx]["y"]),
                    float(poly[idx + 1]["x"]),
                    float(poly[idx + 1]["y"]),
                )
            )
    return lines


def _iter_nearby_edges(
    edge_lines: List[Tuple[float, float, float, float]],
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
) -> Iterable[Tuple[float, float, float, float]]:
    for ax, ay, bx, by in edge_lines:
        if max(ax, bx) < min_x or min(ax, bx) > max_x:
            continue
        if max(ay, by) < min_y or min(ay, by) > max_y:
            continue
        yield ax, ay, bx, by


def _plot_offset(edge_lines: List[Tuple[float, float, float, float]], row: dict, out_path: Path) -> None:
    raw_x = float(row["raw_x"])
    raw_y = float(row["raw_y"])
    projected_x = float(row["projected_x"])
    projected_y = float(row["projected_y"])
    offset = float(row["offset_m"])
    pad = max(250.0, offset * 2.0)
    min_x = min(raw_x, projected_x) - pad
    max_x = max(raw_x, projected_x) + pad
    min_y = min(raw_y, projected_y) - pad
    max_y = max(raw_y, projected_y) + pad

    fig, ax = plt.subplots(figsize=(8, 8), dpi=150)
    segments = [
        [(ax0, ay0), (bx0, by0)]
        for ax0, ay0, bx0, by0 in _iter_nearby_edges(edge_lines, min_x, max_x, min_y, max_y)
    ]
    if segments:
        ax.add_collection(LineCollection(segments, colors="#999999", linewidths=0.45, alpha=0.75, zorder=1))
    ax.scatter([raw_x], [raw_y], marker="*", s=180, color="#d62728", edgecolor="white", linewidth=0.8, zorder=4, label="raw launch")
    ax.scatter([projected_x], [projected_y], marker="o", s=90, color="#1f77b4", edgecolor="white", linewidth=0.8, zorder=5, label="projected road point")
    ax.plot([raw_x, projected_x], [raw_y, projected_y], color="#ff7f0e", linewidth=1.8, linestyle="--", zorder=3)
    title = f"{row['scheduler_id']} {row.get('name') or row.get('index') or row['point_id']} offset {row['offset_m']:.1f}m"
    ax.set_title(title)
    ax.set_xlabel("graph x (m)")
    ax.set_ylabel("graph y (m)")
    ax.legend(loc="upper right")
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(min_y, max_y)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.25)
    fig.subplots_adjust(left=0.10, right=0.98, bottom=0.08, top=0.94)
    fig.savefig(out_path)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit raw launch points against scheduler graph projection points.")
    parser.add_argument("--result-dir", default="result")
    parser.add_argument("--threshold-m", type=float, default=50.0)
    parser.add_argument("--out-dir", default="result/visuals/launch_projection_offsets")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_root = result_dir / "message_capture"
    scheduler_configs = sorted((result_dir / "configs" / "schedulers").glob("scheduler_*.json"))
    all_rows: List[dict] = []
    over_rows: List[dict] = []

    for cfg_path in scheduler_configs:
        cfg = _load_json(cfg_path)
        scheduler_id = str(cfg.get("node_id") or cfg_path.stem)
        capture_path = _latest_capture(capture_root, scheduler_id, "FA_SHE_DIAN")
        if capture_path is None:
            continue
        rows = _normalize_dian_rows(_load_json(capture_path).get("payload") or {})
        graph = MapLoader.load_graph_from_config(cfg["map"])
        projected = _project_launch_rows(graph, rows)
        edge_lines = _edge_xy_lines(graph)
        for row in projected:
            row["scheduler_id"] = scheduler_id
            row["capture_file"] = str(capture_path)
            row["map_graph"] = str(cfg["map"].get("graph_json") or cfg["map"].get("shp_path") or "")
            all_rows.append(row)
            if float(row["offset_m"]) > args.threshold_m:
                over_rows.append(row)
                safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in f"{scheduler_id}_{row.get('index') or row.get('name') or row['row_index']}")
                _plot_offset(edge_lines, row, out_dir / f"{safe_name}_{row['offset_m']:.1f}m.png")

    all_rows.sort(key=lambda row: (row["scheduler_id"], -float(row["offset_m"]), row["row_index"]))
    over_rows.sort(key=lambda row: (row["scheduler_id"], -float(row["offset_m"]), row["row_index"]))
    fieldnames = [
        "scheduler_id",
        "name",
        "index",
        "offset_m",
        "raw_lon",
        "raw_lat",
        "raw_x",
        "raw_y",
        "projected_lon",
        "projected_lat",
        "projected_x",
        "projected_y",
        "point_id",
        "edge_src",
        "edge_dst",
        "edge_ratio",
        "capture_file",
        "map_graph",
    ]
    for path, rows in ((out_dir / "launch_projection_offsets_all.csv", all_rows), (out_dir / "launch_projection_offsets_over_50m.csv", over_rows)):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
    summary = {
        "threshold_m": args.threshold_m,
        "total_points": len(all_rows),
        "over_threshold_count": len(over_rows),
        "max_offset_m": max((float(row["offset_m"]) for row in all_rows), default=0.0),
        "out_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
