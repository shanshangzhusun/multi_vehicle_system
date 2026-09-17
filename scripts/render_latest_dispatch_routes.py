#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".run" / "mplconfig"))

import matplotlib.pyplot as plt

from mvs.scheduler.map_model import MapLoader, RoadGraph


def _latest_dispatch_bundle(log_path: Path) -> Dict[str, Any]:
    marker = "DISPATCH_TRAJECTORY_BUNDLE"
    latest_task_id = ""
    trajectories: List[Dict[str, Any]] = []
    seen_vehicle_ids: set[str] = set()
    raw_obj_pattern = re.compile(r"obj=(\{.*\})")
    payload_pattern = re.compile(r"payload=(\{.*\})")
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if marker not in line:
                continue
            candidates: List[str] = []
            for pattern in (raw_obj_pattern, payload_pattern):
                match = pattern.search(line)
                if match:
                    candidates.append(match.group(1))
            if line.lstrip().startswith("{"):
                candidates.append(line.strip())
            for text in candidates:
                obj = ast.literal_eval(text)
                if obj.get("msg_type") == marker and isinstance(obj.get("data"), dict):
                    bundle = obj["data"]
                elif obj.get("msg_type") == marker:
                    bundle = obj
                else:
                    continue
                task_id = str(bundle.get("task_id") or "")
                if task_id != latest_task_id:
                    latest_task_id = task_id
                    trajectories = []
                    seen_vehicle_ids = set()
                for row in bundle.get("trajectories") or []:
                    if not isinstance(row, dict):
                        continue
                    vehicle_id = str(row.get("vehicle_id") or row.get("port") or "")
                    if vehicle_id in seen_vehicle_ids:
                        continue
                    seen_vehicle_ids.add(vehicle_id)
                    trajectories.append(row)
    if not latest_task_id or not trajectories:
        raise RuntimeError(f"no {marker} line found in {log_path}")
    return {"task_id": latest_task_id, "trajectories": trajectories}


def _load_bundle_file(path: Path) -> Dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise RuntimeError(f"bundle file is not a json object: {path}")
    trajectories = obj.get("trajectories")
    if not isinstance(trajectories, list) or not trajectories:
        raise RuntimeError(f"bundle file has no trajectories: {path}")
    return obj


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    obj = json.loads(path.read_text(encoding="utf-8"))
    data = obj.get("data", obj) if isinstance(obj, dict) else obj
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
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


def _runtime_vehicle_raw_lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        lon = row.get("lon")
        if lon is None:
            lon = row.get("lng")
        if lon is None:
            lon = row.get("longitude")
        if lon is None:
            lon = row.get("platform_LocationLLA_Lon")
        lat = row.get("lat")
        if lat is None:
            lat = row.get("latitude")
        if lat is None:
            lat = row.get("platform_LocationLLA_Lat")
        if lon is None or lat is None:
            return None
        return float(lon), float(lat)
    except Exception:
        return None


def _mapped_lon_lat(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        lon = row.get("mapped_lon")
        lat = row.get("mapped_lat")
        if lon is None or lat is None:
            return None
        return float(lon), float(lat)
    except Exception:
        return None


def _scatter_points(ax: Any, rows: Iterable[Dict[str, Any]], *, label: str, marker: str, color: str) -> None:
    pts = [_lon_lat(row) for row in rows]
    pts = [pt for pt in pts if pt is not None]
    if not pts:
        return
    xs, ys = zip(*pts)
    ax.scatter(xs, ys, s=18, marker=marker, color=color, alpha=0.45, label=label, zorder=1)


def _plot_road_graph(
    ax: Any,
    graph: RoadGraph,
    *,
    bounds: Optional[Tuple[float, float, float, float]] = None,
    emphasized: bool = False,
) -> None:
    drawn = set()
    line_color = "#64748b" if emphasized else "#94a3b8"
    line_width = 0.9 if emphasized else 0.55
    line_alpha = 0.9 if emphasized else 0.65
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
        if bounds is not None:
            min_x, max_x, min_y, max_y = bounds
            if max(xs) < min_x or min(xs) > max_x or max(ys) < min_y or min(ys) > max_y:
                continue
        ax.plot(
            xs,
            ys,
            color=line_color,
            linewidth=line_width,
            alpha=line_alpha,
            zorder=0,
        )


def _load_runtime_point_snapshots(received_dir: Path, suffix: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for path in sorted(received_dir.glob(f"*_{suffix}.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = obj.get("mapped_rows") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("index") or ""),
                str(row.get("name") or ""),
                str(row.get("lon") or row.get("platform_LocationLLA_Lon") or ""),
                str(row.get("lat") or row.get("platform_LocationLLA_Lat") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def _load_runtime_vehicle_rows(received_dir: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(received_dir.glob("*_VEHICLE_CONTEXT.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        vehicle_id = str(obj.get("vehicle_id") or "")
        rows = obj.get("raw_rows") or []
        if not vehicle_id or not isinstance(rows, list) or not rows:
            continue
        first = rows[0]
        if isinstance(first, dict):
            out[vehicle_id] = first
    return out


def _plot_point_group(
    ax: Any,
    raw_rows: Iterable[Dict[str, Any]],
    *,
    raw_label: str,
    raw_marker: str,
    raw_color: str,
    projected_label: str,
    projected_marker: str,
    projected_color: str,
) -> None:
    raw_pts = [_lon_lat(row) for row in raw_rows]
    raw_pts = [pt for pt in raw_pts if pt is not None]
    proj_pts = [_mapped_lon_lat(row) for row in raw_rows]
    proj_pts = [pt for pt in proj_pts if pt is not None]
    if raw_pts:
        xs, ys = zip(*raw_pts)
        ax.scatter(xs, ys, s=24, marker=raw_marker, color=raw_color, alpha=0.55, label=raw_label, zorder=1)
    if proj_pts:
        xs, ys = zip(*proj_pts)
        ax.scatter(xs, ys, s=28, marker=projected_marker, color=projected_color, alpha=0.9, label=projected_label, zorder=2)
    for row in raw_rows:
        raw_pt = _lon_lat(row)
        proj_pt = _mapped_lon_lat(row)
        if raw_pt is not None and proj_pt is not None:
            ax.plot(
                [raw_pt[0], proj_pt[0]],
                [raw_pt[1], proj_pt[1]],
                color=projected_color,
                linewidth=0.6,
                alpha=0.35,
                zorder=1,
            )


def _apply_focus_limits(ax: Any, pts: List[Tuple[float, float]]) -> None:
    if not pts:
        return
    xs = [pt[0] for pt in pts]
    ys = [pt[1] for pt in pts]
    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    dx = max(max_x - min_x, 0.01)
    dy = max(max_y - min_y, 0.01)
    pad_x = dx * 0.12
    pad_y = dy * 0.12
    ax.set_xlim(min_x - pad_x, max_x + pad_x)
    ax.set_ylim(min_y - pad_y, max_y + pad_y)


def _focus_bounds(pts: List[Tuple[float, float]]) -> Optional[Tuple[float, float, float, float]]:
    if not pts:
        return None
    xs = [pt[0] for pt in pts]
    ys = [pt[1] for pt in pts]
    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    dx = max(max_x - min_x, 0.01)
    dy = max(max_y - min_y, 0.01)
    pad_x = dx * 0.12
    pad_y = dy * 0.12
    return (min_x - pad_x, max_x + pad_x, min_y - pad_y, max_y + pad_y)


def _trajectory_bad_reason(traj: Dict[str, Any]) -> str:
    points = traj.get("path_points") or traj.get("trajectory") or traj.get("trajectory_geo") or []
    if not isinstance(points, list) or len(points) <= 1:
        return "too_few_points"
    fire_error = traj.get("fire_time_error_sec")
    if fire_error is not None:
        try:
            error = float(fire_error)
        except Exception:
            error = 0.0
        if abs(error) > 60.0:
            return f"fire_time_error={round(error, 3)}"
    return ""


def _conflict_failed(traj: Dict[str, Any]) -> bool:
    source = str(traj.get("dispatch_selection_source") or "")
    return traj.get("conflict_resolved") is False or (
        bool(source) and source not in {"conflict_free", "conflict_free_reassigned"}
    )


def render(
    bundle: Dict[str, Any],
    dian_dir: Path,
    received_dir: Path,
    graph: RoadGraph,
    out_path: Path,
    *,
    focus_routes_only: bool = False,
) -> None:
    trajectories = bundle.get("trajectories") or []
    if not isinstance(trajectories, list) or not trajectories:
        raise RuntimeError("dispatch bundle has no trajectories")
    unresolved_vehicle_ids = [str(item) for item in bundle.get("unresolved_vehicle_ids") or []]

    fire_rows = _load_runtime_point_snapshots(received_dir, "FA_SHE_DIAN")
    if not fire_rows:
        fire_rows = _load_preferred_rows(dian_dir / "FA_SHE_DIAN.json", dian_dir / "FA_SHE_DIAN_64.json")
    hide_rows = _load_runtime_point_snapshots(received_dir, "YIN_BI_DIAN")
    if not hide_rows:
        hide_rows = _load_preferred_rows(dian_dir / "YIN_BI_DIAN.json", dian_dir / "YIN_BI_DIAN_64.json")

    runtime_vehicle_rows = _load_runtime_vehicle_rows(received_dir)
    focus_pts: List[Tuple[float, float]] = []
    if not focus_routes_only:
        for row in fire_rows:
            pt = _lon_lat(row)
            mapped = _mapped_lon_lat(row)
            if pt is not None:
                focus_pts.append(pt)
            if mapped is not None:
                focus_pts.append(mapped)
        for row in hide_rows:
            pt = _lon_lat(row)
            mapped = _mapped_lon_lat(row)
            if pt is not None:
                focus_pts.append(pt)
            if mapped is not None:
                focus_pts.append(mapped)

    cmap = plt.get_cmap("tab20")
    bad: List[str] = []
    conflict_failed: List[str] = []
    for idx, traj in enumerate(trajectories):
        if not isinstance(traj, dict):
            continue
        vehicle_id = str(traj.get("vehicle_id") or traj.get("port") or f"veh_{idx}")
        if _conflict_failed(traj):
            conflict_failed.append(vehicle_id)
        points = traj.get("path_points") or traj.get("trajectory") or traj.get("trajectory_geo") or []
        pts = [_lon_lat(pt) for pt in points if isinstance(pt, dict)]
        pts = [pt for pt in pts if pt is not None]
        color = cmap(idx % cmap.N)
        bad_reason = _trajectory_bad_reason(traj)
        if bad_reason:
            bad.append(f"{vehicle_id}({bad_reason})")
        if len(pts) <= 1:
            continue
        raw_vehicle = runtime_vehicle_rows.get(vehicle_id)
        raw_vehicle_pt = _runtime_vehicle_raw_lon_lat(raw_vehicle) if raw_vehicle else None
        if raw_vehicle_pt is not None:
            focus_pts.append(raw_vehicle_pt)
        focus_pts.extend(pts)

    bounds = _focus_bounds(focus_pts) if focus_routes_only else None
    fig, ax = plt.subplots(figsize=(13, 9), dpi=160)
    _plot_road_graph(ax, graph, bounds=bounds, emphasized=focus_routes_only)

    _plot_point_group(
        ax,
        fire_rows,
        raw_label="fire raw",
        raw_marker="^",
        raw_color="#fdba74",
        projected_label="fire projected",
        projected_marker="^",
        projected_color="#d95f02",
    )
    _plot_point_group(
        ax,
        hide_rows,
        raw_label="hide raw",
        raw_marker="s",
        raw_color="#c4b5fd",
        projected_label="hide projected",
        projected_marker="s",
        projected_color="#6d28d9",
    )

    for idx, traj in enumerate(trajectories):
        if not isinstance(traj, dict):
            continue
        vehicle_id = str(traj.get("vehicle_id") or traj.get("port") or f"veh_{idx}")
        conflict_failed_path = _conflict_failed(traj)
        startup_mode = str(traj.get("launch_startup_mode") or "").lower()
        mode_suffix = f" {startup_mode}" if startup_mode else ""
        bad_reason = _trajectory_bad_reason(traj)
        points = traj.get("path_points") or traj.get("trajectory") or traj.get("trajectory_geo") or []
        pts = [_lon_lat(pt) for pt in points if isinstance(pt, dict)]
        pts = [pt for pt in pts if pt is not None]
        color = cmap(idx % cmap.N)
        if len(pts) <= 1:
            if pts:
                ax.scatter([pts[0][0]], [pts[0][1]], s=95, marker="x", linewidths=2.6, color="red", zorder=5)
                ax.text(pts[0][0], pts[0][1], f" {vehicle_id} BAD {bad_reason}", fontsize=8, color="red")
            continue
        raw_vehicle = runtime_vehicle_rows.get(vehicle_id)
        raw_vehicle_pt = _runtime_vehicle_raw_lon_lat(raw_vehicle) if raw_vehicle else None
        if raw_vehicle_pt is not None:
            ax.scatter(
                [raw_vehicle_pt[0]],
                [raw_vehicle_pt[1]],
                s=34,
                marker="o",
                facecolors="none",
                edgecolors=color,
                linewidths=1.2,
                zorder=4,
                label="vehicle raw" if idx == 0 else None,
            )
            ax.plot(
                [raw_vehicle_pt[0], pts[0][0]],
                [raw_vehicle_pt[1], pts[0][1]],
                color=color,
                linewidth=0.8,
                alpha=0.45,
                linestyle="--",
                zorder=3,
            )
        xs, ys = zip(*pts)
        ax.plot(
            xs,
            ys,
            linewidth=1.5,
            color=color,
            linestyle="--" if conflict_failed_path else "-",
            label=f"{vehicle_id} ({len(pts)})" + (" conflict" if conflict_failed_path else ""),
            zorder=3,
        )
        ax.scatter([xs[0]], [ys[0]], s=30, marker="o", color=color, zorder=5, label="vehicle projected" if idx == 0 else None)
        ax.scatter([xs[-1]], [ys[-1]], s=45, marker="*", color=color, zorder=4)
        flags = []
        if conflict_failed_path:
            flags.append("CONFLICT")
        if bad_reason:
            flags.append("BAD")
        label_suffix = (" " + " ".join(flags)) if flags else mode_suffix
        ax.text(xs[-1], ys[-1], f" {vehicle_id}{label_suffix}", fontsize=8, color="red" if flags else color)

    ax.set_title(
        f"Dispatch Routes task={bundle.get('task_id', '')} trajectories={len(trajectories)} "
        f"bad={len(bad)} conflict_failed={len(conflict_failed)} unresolved={len(unresolved_vehicle_ids)}"
    )
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.grid(True, alpha=0.25)
    ax.set_aspect("equal", adjustable="box")
    if focus_routes_only:
        _apply_focus_limits(ax, focus_pts)
    ax.legend(loc="best", fontsize=7, ncols=2)
    if bad:
        ax.text(
            0.01,
            0.01,
            "bad paths: " + ", ".join(bad),
            transform=ax.transAxes,
            color="red",
            fontsize=10,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "red"},
        )
    if conflict_failed:
        ax.text(
            0.01,
            0.07 if bad else 0.01,
            "conflict failed: " + ", ".join(conflict_failed),
            transform=ax.transAxes,
            color="red",
            fontsize=10,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "red"},
        )
    if unresolved_vehicle_ids:
        ax.text(
            0.01,
            0.13 if bad and conflict_failed else (0.07 if bad or conflict_failed else 0.01),
            "unresolved: " + ", ".join(unresolved_vehicle_ids),
            transform=ax.transAxes,
            color="red",
            fontsize=10,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "red"},
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render latest DISPATCH_TRAJECTORY_BUNDLE from manual_debug.log")
    parser.add_argument("--log", default=".run/logs/manual_debug.log")
    parser.add_argument("--bundle", default="result/latest_dispatch_trajectory_bundle.json")
    parser.add_argument("--dian-dir", default="result/extracted_dian")
    parser.add_argument("--received-dir", default="result/received_dian")
    parser.add_argument("--scheduler-config", default="result/configs/scheduler_debug.json")
    parser.add_argument("--out", default="result/visuals/latest_dispatch_routes.png")
    parser.add_argument("--focus-routes-only", action="store_true")
    args = parser.parse_args()

    bundle_path = Path(args.bundle)
    if bundle_path.exists():
        bundle = _load_bundle_file(bundle_path)
    else:
        bundle = _latest_dispatch_bundle(Path(args.log))
    cfg = json.loads(Path(args.scheduler_config).read_text(encoding="utf-8"))
    graph = MapLoader.load_graph_from_config(cfg["map"])
    render(
        bundle,
        Path(args.dian_dir),
        Path(args.received_dir),
        graph,
        Path(args.out),
        focus_routes_only=args.focus_routes_only,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
