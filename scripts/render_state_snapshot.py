#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, Tuple

from PIL import Image, ImageDraw


def bounds_from_state(state: dict) -> Tuple[float, float, float, float]:
    xs = [float(n["x"]) for n in state["map"]["nodes"]]
    ys = [float(n["y"]) for n in state["map"]["nodes"]]
    return min(xs), min(ys), max(xs), max(ys)


def project_factory(bounds: Tuple[float, float, float, float], width: int, height: int, padding: int = 40):
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1.0)
    dy = max(maxy - miny, 1.0)
    scale = min((width - 2 * padding) / dx, (height - 2 * padding) / dy)
    xoff = padding + (width - 2 * padding - dx * scale) / 2.0
    yoff = padding + (height - 2 * padding - dy * scale) / 2.0

    def project(x: float, y: float) -> Tuple[int, int]:
        px = xoff + (x - minx) * scale
        py = height - (yoff + (y - miny) * scale)
        return int(round(px)), int(round(py))

    return project


def bbox_to_canvas(project, bbox: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
    minx, miny, maxx, maxy = bbox
    p1 = project(minx, miny)
    p2 = project(maxx, maxy)
    left = min(p1[0], p2[0])
    right = max(p1[0], p2[0])
    top = min(p1[1], p2[1])
    bottom = max(p1[1], p2[1])
    return left, top, right, bottom


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def draw_polyline(draw: ImageDraw.ImageDraw, pts, color: str, width: int) -> None:
    if len(pts) >= 2:
        draw.line(pts, fill=color, width=width, joint="curve")


def node_lookup(state: dict) -> Dict[str, dict]:
    out = {}
    for n in state["map"]["nodes"]:
        node_id = n.get("node_id", n.get("id"))
        if node_id:
            out[str(node_id)] = n
    return out


def point_positions(state: dict, nodes_by_id: Dict[str, dict]) -> dict:
    out = {"hide_points": [], "launch_points": [], "depots": []}
    for key in out:
        for node_id in state["points"].get(key, []):
            node = nodes_by_id.get(node_id)
            if node:
                out[key].append((float(node["x"]), float(node["y"]), node_id))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a PNG snapshot from scheduler /api/state json")
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1100)
    args = parser.parse_args()

    state = load_state(Path(args.state_json))
    image = Image.new("RGB", (args.width, args.height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    bounds = bounds_from_state(state)
    project = project_factory(bounds, args.width, args.height)
    nodes_by_id = node_lookup(state)
    fire_zone = state.get("fire_zone") or {}
    fire_bbox = fire_zone.get("bbox")

    if fire_zone.get("enabled") and isinstance(fire_bbox, list) and len(fire_bbox) == 4:
        l, t, r, b = bbox_to_canvas(project, tuple(float(v) for v in fire_bbox))
        draw.rectangle((l, t, r, b), fill="#fee2e266", outline="#dc2626")

    for edge in state["map"]["edges"]:
        geom = edge.get("geometry") or []
        if not geom:
            a = nodes_by_id.get(edge["from"])
            b = nodes_by_id.get(edge["to"])
            if not a or not b:
                continue
            geom = [{"x": a["x"], "y": a["y"]}, {"x": b["x"], "y": b["y"]}]
        pts = [project(float(p["x"]), float(p["y"])) for p in geom]
        draw_polyline(draw, pts, "#cbd5e1", 1)

    for vehicle in state.get("vehicles", []):
        plan = vehicle.get("active_plan") or {}
        traj = plan.get("trajectory") or []
        if traj:
            pts = [project(float(p["x"]), float(p["y"])) for p in traj if "x" in p and "y" in p]
            draw_polyline(draw, pts, "#93c5fd", 3)

    pts = point_positions(state, nodes_by_id)
    for x, y, _ in pts["hide_points"]:
        px, py = project(x, y)
        draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill="#16a34a", outline="#14532d")
    for x, y, _ in pts["launch_points"]:
        px, py = project(x, y)
        draw.rectangle((px - 5, py - 5, px + 5, py + 5), fill="#ef4444", outline="#7f1d1d")
    for x, y, _ in pts["depots"]:
        px, py = project(x, y)
        draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill="#f59e0b", outline="#78350f")

    for vehicle in state.get("vehicles", []):
        if "current_x" in vehicle and "current_y" in vehicle:
            x = float(vehicle["current_x"])
            y = float(vehicle["current_y"])
        else:
            node = nodes_by_id.get(vehicle.get("current_node", ""))
            if not node:
                continue
            x = float(node["x"])
            y = float(node["y"])
        px, py = project(x, y)
        online = bool(vehicle.get("online"))
        fill = "#2563eb" if online else "#94a3b8"
        draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=fill, outline="#0f172a")
        draw.text((px + 8, py - 8), vehicle["vehicle_id"], fill="#0f172a")

    draw.rectangle((12, 12, args.width - 12, 92), fill="#ffffff", outline="#e2e8f0")
    metrics = state.get("metrics", {})
    title = "SHP Demo Runtime Snapshot"
    subtitle = (
        f"online={metrics.get('vehicles_online', 0)}/{metrics.get('vehicles_total', 0)}  "
        f"subtasks_total={metrics.get('subtasks_total', 0)}  "
        f"pending={metrics.get('subtasks_pending', 0)}  "
        f"running={metrics.get('subtasks_running', 0)}  "
        f"fire_zone={'on' if fire_zone.get('enabled') else 'off'}"
    )
    draw.text((24, 24), title, fill="#0f172a")
    draw.text((24, 50), subtitle, fill="#475569")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    print(f"saved snapshot to {out_path}")


if __name__ == "__main__":
    main()
