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


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_rows(path: Path) -> List[Dict[str, Any]]:
    obj = _load_json(path)
    rows = obj.get("raw_rows") if isinstance(obj, dict) else None
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _best_packet_rows(received_dir: Path, msg_type: str) -> List[Dict[str, Any]]:
    paths = sorted(received_dir.glob(f"*_{msg_type}.json"), key=lambda p: (p.stat().st_mtime, p.name))
    if not paths:
        return []
    best = max(paths, key=lambda p: (len(_raw_rows(p)), p.stat().st_mtime))
    return _raw_rows(best)


def _vehicle_context_rows(received_dir: Path) -> List[Dict[str, Any]]:
    by_vehicle: Dict[str, Dict[str, Any]] = {}
    for path in sorted(received_dir.glob("*_VEHICLE_CONTEXT.json"), key=lambda p: (p.stat().st_mtime, p.name)):
        for row in _raw_rows(path):
            key = str(row.get("vehicle_id") or row.get("port") or path.name.split("_", 1)[0])
            by_vehicle[key] = row
    return [by_vehicle[key] for key in sorted(by_vehicle, key=lambda v: int(v) if v.isdigit() else v)]


def _lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    lon = row.get("lon", row.get("lng", row.get("longitude", row.get("platform_LocationLLA_Lon"))))
    lat = row.get("lat", row.get("latitude", row.get("platform_LocationLLA_Lat")))
    try:
        if lon is None or lat is None:
            return None
        return float(lon), float(lat)
    except Exception:
        return None


def _label(row: Dict[str, Any], kind: str) -> str:
    if kind == "vehicle":
        return str(row.get("vehicle_id") or row.get("port") or row.get("platform_Index") or row.get("index") or "?")
    return str(row.get("index") if row.get("index") is not None else row.get("name") or "?")


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
                color="#64748b",
                linewidth=0.4,
                alpha=0.45,
                zorder=1,
            )


def _points(rows: Iterable[Dict[str, Any]], kind: str) -> List[Tuple[float, float, str]]:
    pts: List[Tuple[float, float, str]] = []
    for row in rows:
        xy = _lon_lat(row)
        if xy is None:
            continue
        pts.append((xy[0], xy[1], _label(row, kind)))
    return pts


def main() -> None:
    parser = argparse.ArgumentParser(description="Render received vehicle/fire/hide points on one map.")
    parser.add_argument("--scheduler-config", default="result/configs/schedulers/scheduler_001.json")
    parser.add_argument("--received-dir", default="result/received_dian")
    parser.add_argument("--out", default="result/visuals/received_dian_all_points.png")
    args = parser.parse_args()

    cfg = _load_json(Path(args.scheduler_config))
    graph = MapLoader.load_graph_from_config(cfg["map"])
    received_dir = Path(args.received_dir)

    vehicle_pts = _points(_vehicle_context_rows(received_dir), "vehicle")
    fire_pts = _points(_best_packet_rows(received_dir, "FA_SHE_DIAN"), "fire")
    hide_pts = _points(_best_packet_rows(received_dir, "YIN_BI_DIAN"), "hide")
    all_pts = vehicle_pts + fire_pts + hide_pts
    if not all_pts:
        raise RuntimeError(f"no plottable points from {received_dir}")

    fig, ax = plt.subplots(figsize=(18, 12), dpi=180)
    _plot_road_graph(ax, graph)

    def draw(pts: List[Tuple[float, float, str]], marker: str, color: str, label: str, text_color: str) -> None:
        if not pts:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.scatter(xs, ys, s=36, marker=marker, color=color, alpha=0.86, zorder=4, label=f"{label} ({len(pts)})")
        dx = max((max(x for x, _, _ in all_pts) - min(x for x, _, _ in all_pts)) * 0.002, 0.001)
        dy = max((max(y for _, y, _ in all_pts) - min(y for _, y, _ in all_pts)) * 0.002, 0.001)
        for x, y, text in pts:
            ax.text(x + dx, y + dy, text, fontsize=6, color=text_color, zorder=5)

    draw(fire_pts, "^", "#ea580c", "fire", "#9a3412")
    draw(hide_pts, "s", "#7c3aed", "hide", "#4c1d95")
    draw(vehicle_pts, "o", "#0284c7", "vehicle", "#075985")

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    pad_x = max((max(xs) - min(xs)) * 0.08, 0.02)
    pad_y = max((max(ys) - min(ys)) * 0.08, 0.02)
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="best", fontsize=8)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"Received Dian Points vehicle={len(vehicle_pts)} fire={len(fire_pts)} hide={len(hide_pts)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(out)


if __name__ == "__main__":
    main()
