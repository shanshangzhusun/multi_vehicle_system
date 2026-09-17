#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".run" / "mplconfig"))

import matplotlib.pyplot as plt

from mvs.scheduler.map_model import MapLoader, RoadGraph


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    obj = json.loads(path.read_text(encoding="utf-8"))
    data = obj.get("data", obj) if isinstance(obj, dict) else obj
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _load_preferred_rows(primary: Path, fallback: Path) -> List[Dict[str, Any]]:
    rows = _load_rows(primary)
    if rows:
        return rows
    return _load_rows(fallback)


def _lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        lat = row.get("lat", row.get("latitude"))
        if lon is None or lat is None:
            return None
        return float(lon), float(lat)
    except Exception:
        return None


def _label(row: Dict[str, Any], kind: str) -> str:
    if row.get("index") is not None:
        return str(row.get("index"))
    if kind == "vehicle":
        return str(row.get("vehicle_id") or row.get("port") or row.get("name") or "?")
    return str(row.get("name") or "?")


def _plot_road_graph(ax: Any, graph: RoadGraph) -> None:
    drawn = set()
    for src_id, neighbors in graph.edges.items():
        src = graph.nodes.get(src_id)
        if src is None or src.lon is None or src.lat is None or not isinstance(neighbors, list):
            continue
        for item in neighbors:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            dst_id = item[0]
            dst = graph.nodes.get(dst_id)
            if dst is None or dst.lon is None or dst.lat is None:
                continue
            edge_key = tuple(sorted((str(src_id), str(dst_id))))
            if edge_key in drawn:
                continue
            drawn.add(edge_key)
            ax.plot(
                [float(src.lon), float(dst.lon)],
                [float(src.lat), float(dst.lat)],
                color="#cbd5e1",
                linewidth=0.35,
                alpha=0.45,
                zorder=1,
            )


def _render_one(
    graph: RoadGraph,
    rows: Iterable[Dict[str, Any]],
    *,
    title: str,
    kind: str,
    marker: str,
    color: str,
    out_path: Path,
) -> None:
    pts: List[Tuple[float, float, str]] = []
    for row in rows:
        xy = _lon_lat(row)
        if xy is None:
            continue
        pts.append((xy[0], xy[1], _label(row, kind)))
    if not pts:
        raise RuntimeError(f"no plottable points for {title}")

    fig, ax = plt.subplots(figsize=(14, 10), dpi=180)
    _plot_road_graph(ax, graph)

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ax.scatter(xs, ys, s=26, marker=marker, color=color, alpha=0.9, zorder=3)

    dx = max((max(xs) - min(xs)) * 0.004, 0.0015)
    dy = max((max(ys) - min(ys)) * 0.004, 0.0015)
    for x, y, text in pts:
        ax.text(x + dx, y + dy, text, fontsize=7, color="#111827", zorder=4)

    pad_x = max((max(xs) - min(xs)) * 0.08, 0.02)
    pad_y = max((max(ys) - min(ys)) * 0.08, 0.02)
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.2)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{title} count={len(pts)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _render_all(
    graph: RoadGraph,
    fire_rows: Iterable[Dict[str, Any]],
    hide_rows: Iterable[Dict[str, Any]],
    vehicle_rows: Iterable[Dict[str, Any]],
    *,
    out_path: Path,
) -> None:
    fire_pts: List[Tuple[float, float, str]] = []
    hide_pts: List[Tuple[float, float, str]] = []
    vehicle_pts: List[Tuple[float, float, str]] = []
    for row in fire_rows:
        xy = _lon_lat(row)
        if xy is not None:
            fire_pts.append((xy[0], xy[1], _label(row, "fire")))
    for row in hide_rows:
        xy = _lon_lat(row)
        if xy is not None:
            hide_pts.append((xy[0], xy[1], _label(row, "hide")))
    for row in vehicle_rows:
        xy = _lon_lat(row)
        if xy is not None:
            vehicle_pts.append((xy[0], xy[1], _label(row, "vehicle")))
    all_pts = fire_pts + hide_pts + vehicle_pts
    if not all_pts:
        raise RuntimeError("no plottable points for overview")

    fig, ax = plt.subplots(figsize=(18, 12), dpi=180)
    _plot_road_graph(ax, graph)

    def draw(
        pts: List[Tuple[float, float, str]],
        *,
        marker: str,
        color: str,
        label: str,
        text_color: str,
    ) -> None:
        if not pts:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.scatter(xs, ys, s=32, marker=marker, color=color, alpha=0.88, zorder=4, label=f"{label} ({len(pts)})")

        dx = max((max(x for x, _, _ in all_pts) - min(x for x, _, _ in all_pts)) * 0.003, 0.0015)
        dy = max((max(y for _, y, _ in all_pts) - min(y for _, y, _ in all_pts)) * 0.003, 0.0015)
        for x, y, text in pts:
            ax.text(
                x + dx,
                y + dy,
                text,
                fontsize=6,
                color=text_color,
                zorder=5,
                bbox={"boxstyle": "round,pad=0.1", "facecolor": "white", "edgecolor": "none", "alpha": 0.65},
            )

    draw(fire_pts, marker="^", color="#d95f02", label="fire", text_color="#9a3412")
    draw(hide_pts, marker="s", color="#6d28d9", label="hide", text_color="#4c1d95")
    draw(vehicle_pts, marker="o", color="#0284c7", label="vehicle", text_color="#075985")

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    pad_x = max((max(xs) - min(xs)) * 0.06, 0.02)
    pad_y = max((max(ys) - min(ys)) * 0.06, 0.02)
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.18)
    ax.legend(loc="best", fontsize=8)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(
        f"All Dian Points fire={len(fire_pts)} hide={len(hide_pts)} vehicle={len(vehicle_pts)}"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render fire/hide/vehicle dian points separately with labels.")
    parser.add_argument("--scheduler-config", default="result/configs/scheduler_debug.json")
    parser.add_argument("--dian-dir", default="result/extracted_dian")
    parser.add_argument("--out-dir", default="result/visuals/dian_separate")
    args = parser.parse_args()

    cfg = json.loads(Path(args.scheduler_config).read_text(encoding="utf-8"))
    graph = MapLoader.load_graph_from_config(cfg["map"])
    dian_dir = Path(args.dian_dir)
    out_dir = Path(args.out_dir)

    fire_rows = _load_preferred_rows(dian_dir / "FA_SHE_DIAN.json", dian_dir / "FA_SHE_DIAN_64.json")
    hide_rows = _load_preferred_rows(dian_dir / "YIN_BI_DIAN.json", dian_dir / "YIN_BI_DIAN_64.json")
    vehicle_rows = _load_preferred_rows(dian_dir / "VEHICLE_DIAN.json", dian_dir / "VEHICLE_DIAN_16.json")

    _render_one(graph, fire_rows, title="Fire Points", kind="fire", marker="^", color="#d95f02", out_path=out_dir / "fire_points.png")
    _render_one(graph, hide_rows, title="Hide Points", kind="hide", marker="s", color="#7570b3", out_path=out_dir / "hide_points.png")
    _render_one(graph, vehicle_rows, title="Vehicle Points", kind="vehicle", marker="o", color="#1b9e77", out_path=out_dir / "vehicle_points.png")
    _render_all(graph, fire_rows, hide_rows, vehicle_rows, out_path=out_dir / "all_points.png")

    print(out_dir / "fire_points.png")
    print(out_dir / "hide_points.png")
    print(out_dir / "vehicle_points.png")
    print(out_dir / "all_points.png")


if __name__ == "__main__":
    main()
