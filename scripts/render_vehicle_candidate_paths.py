#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
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


def _candidate_payloads(log_path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    marker = "raw_json_send msg_type=VEHICLE_CANDIDATE_PATH_RESULT"
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if marker not in line or "payload=" not in line:
                continue
            try:
                payload = ast.literal_eval(line.split("payload=", 1)[1].strip())
            except Exception:
                continue
            if isinstance(payload, dict):
                out.append(payload)
    return out


def _latest_payload_for_vehicle(log_path: Path, vehicle_id: str) -> Dict[str, Any]:
    normalized = str(vehicle_id)
    matched = [
        payload
        for payload in _candidate_payloads(log_path)
        if str(payload.get("vehicle_id") or payload.get("port")) == normalized
    ]
    if not matched:
        raise RuntimeError(f"no VEHICLE_CANDIDATE_PATH_RESULT found for vehicle {vehicle_id} in {log_path}")
    return matched[-1]


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


def _load_vehicle_row(dian_dir: Path, vehicle_id: str) -> Optional[Dict[str, Any]]:
    rows = _load_rows(dian_dir / "VEHICLE_DIAN.json") + _load_rows(dian_dir / "VEHICLE_DIAN_16.json")
    want = str(vehicle_id)
    for row in rows:
        key = str(row.get("vehicle_id") or row.get("port") or row.get("index") or "")
        name = str(row.get("name") or "")
        if key == want or name.endswith(f"_{int(want) - 8414}") or want in name:
            return row
    return None


def _lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        lat = row.get("lat", row.get("latitude"))
        if lon is None or lat is None:
            return None
        return float(lon), float(lat)
    except Exception:
        return None


def _scatter_points(ax: Any, rows: Iterable[Dict[str, Any]], *, label: str, marker: str, color: str, size: int, alpha: float = 0.6) -> None:
    pts = [_lon_lat(row) for row in rows]
    pts = [pt for pt in pts if pt is not None]
    if not pts:
        return
    xs, ys = zip(*pts)
    ax.scatter(xs, ys, s=size, marker=marker, color=color, alpha=alpha, label=label, zorder=2)


def _plot_road_graph(ax: Any, graph: RoadGraph) -> None:
    drawn = set()
    for edge_key, meta in graph.edge_meta.items():
        if edge_key in drawn:
            continue
        drawn.add(edge_key)
        geometry = list(meta.geometry or [])
        geom_pts = [
            (float(pt["lon"]), float(pt["lat"]))
            for pt in geometry
            if isinstance(pt, dict) and pt.get("lon") is not None and pt.get("lat") is not None
        ]
        if len(geom_pts) >= 2:
            xs, ys = zip(*geom_pts)
        else:
            src = graph.nodes.get(meta.src)
            dst = graph.nodes.get(meta.dst)
            if src is None or dst is None or src.lon is None or src.lat is None or dst.lon is None or dst.lat is None:
                continue
            xs = (float(src.lon), float(dst.lon))
            ys = (float(src.lat), float(dst.lat))
        ax.plot(
            xs,
            ys,
            color="#94a3b8",
            linewidth=0.55,
            alpha=0.7,
            zorder=1,
        )


def _candidate_title(path: Dict[str, Any]) -> str:
    return (
        f"rank={path.get('rank')} "
        f"score={path.get('score_total')} "
        f"strategy={path.get('timing_strategy')} "
        f"hide={path.get('hide_selected')} "
        f"travel={path.get('travel_seconds')}"
    )


def _render_one(
    graph: RoadGraph,
    vehicle_id: str,
    vehicle_row: Optional[Dict[str, Any]],
    fire_rows: List[Dict[str, Any]],
    hide_rows: List[Dict[str, Any]],
    path: Dict[str, Any],
    out_path: Path,
) -> None:
    pts = [row for row in path.get("path_points") or [] if isinstance(row, dict)]
    xy = [_lon_lat(row) for row in pts]
    xy = [pt for pt in xy if pt is not None]
    if not xy:
        raise RuntimeError(f"candidate path has no plottable points for vehicle {vehicle_id}")

    fig, ax = plt.subplots(figsize=(12, 9), dpi=160)
    _plot_road_graph(ax, graph)
    _scatter_points(ax, fire_rows, label="fire points", marker="^", color="#d95f02", size=20, alpha=0.55)
    _scatter_points(ax, hide_rows, label="hide points", marker="s", color="#7570b3", size=16, alpha=0.45)
    if vehicle_row:
        _scatter_points(ax, [vehicle_row], label=f"vehicle {vehicle_id}", marker="o", color="#1b9e77", size=55, alpha=0.95)

    xs, ys = zip(*xy)
    ax.plot(xs, ys, color="#e41a1c", linewidth=2.4, zorder=4, label=f"path rank {path.get('rank')}")
    ax.scatter([xs[0]], [ys[0]], s=70, marker="o", color="#2c7fb8", zorder=5, label="start")
    ax.scatter([xs[-1]], [ys[-1]], s=90, marker="*", color="#d7301f", zorder=5, label="end")

    if path.get("wait_seconds", 0.0):
        wait_idx = None
        if pts:
            wait_start = path.get("wait_start_time")
            wait_end = path.get("wait_end_time")
            for idx, row in enumerate(pts):
                t = row.get("time")
                if t is None:
                    continue
                try:
                    tf = float(t)
                except Exception:
                    continue
                if wait_start is not None and wait_end is not None and float(wait_start) <= tf <= float(wait_end):
                    wait_idx = idx
                    break
        if wait_idx is not None:
            ax.scatter([xs[wait_idx]], [ys[wait_idx]], s=90, marker="P", color="#54278f", zorder=6, label="wait point")

    ax.set_title(f"Vehicle {vehicle_id} Candidate Route\n{_candidate_title(path)}")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.grid(True, alpha=0.22)
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="best", fontsize=8)

    pad_x = max((max(xs) - min(xs)) * 0.08, 0.01)
    pad_y = max((max(ys) - min(ys)) * 0.08, 0.01)
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render one vehicle's 1-3 candidate path images from manual_debug.log")
    parser.add_argument("--vehicle-id", required=True, help="vehicle port/id, e.g. 8418")
    parser.add_argument("--log", default=".run/logs/manual_debug.log")
    parser.add_argument("--dian-dir", default="result/extracted_dian")
    parser.add_argument("--vehicle-config", default=None, help="defaults to result/configs/vehicles/<vehicle-id>.json")
    parser.add_argument("--out-dir", default="result/visuals/candidate_paths")
    args = parser.parse_args()

    vehicle_id = str(args.vehicle_id)
    log_path = Path(args.log)
    dian_dir = Path(args.dian_dir)
    cfg_path = Path(args.vehicle_config) if args.vehicle_config else Path(f"result/configs/vehicles/{vehicle_id}.json")
    if not cfg_path.exists():
        raise SystemExit(f"vehicle config not found: {cfg_path}")

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    graph = MapLoader.load_graph_from_config(cfg["map"])
    payload = _latest_payload_for_vehicle(log_path, vehicle_id)
    paths = payload.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise SystemExit(f"latest payload for vehicle {vehicle_id} has no paths")

    fire_rows = _load_rows(dian_dir / "FA_SHE_DIAN.json") or _load_rows(dian_dir / "FA_SHE_DIAN_64.json")
    hide_rows = _load_rows(dian_dir / "YIN_BI_DIAN.json") or _load_rows(dian_dir / "YIN_BI_DIAN_64.json")
    vehicle_row = _load_vehicle_row(dian_dir, vehicle_id)

    out_dir = Path(args.out_dir) / vehicle_id
    wrote = []
    for idx, path in enumerate(paths[:6], start=1):
        out_path = out_dir / f"{vehicle_id}_candidate_{idx}.png"
        _render_one(graph, vehicle_id, vehicle_row, fire_rows, hide_rows, path, out_path)
        wrote.append(out_path)

    print(f"vehicle={vehicle_id} candidate_paths={len(paths)} rendered={len(wrote)}")
    for path in wrote:
        print(path)


if __name__ == "__main__":
    main()
