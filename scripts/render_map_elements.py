#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.scheduler.map_model import MapLoader


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


def graph_bounds(graph) -> Tuple[float, float, float, float]:
    xs = [n.x for n in graph.nodes.values()]
    ys = [n.y for n in graph.nodes.values()]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_to_canvas(project, bbox: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
    minx, miny, maxx, maxy = bbox
    p1 = project(minx, miny)
    p2 = project(maxx, maxy)
    left = min(p1[0], p2[0])
    right = max(p1[0], p2[0])
    top = min(p1[1], p2[1])
    bottom = max(p1[1], p2[1])
    return left, top, right, bottom


def draw_diamond(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: str, outline: str) -> None:
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill, outline=outline)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render map elements from a scheduler config into a PNG image.")
    parser.add_argument("--scheduler-config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1100)
    args = parser.parse_args()

    cfg = json.loads(Path(args.scheduler_config).read_text(encoding="utf-8"))
    graph = MapLoader.load_graph_from_config(cfg["map"])
    points = MapLoader.load_points_from_config(cfg["map"], graph)
    homes = [v["home_node"] for v in cfg.get("vehicles", []) if v.get("home_node") in graph.nodes]

    image = Image.new("RGB", (args.width, args.height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    bounds = graph_bounds(graph)
    project = project_factory(bounds, args.width, args.height)

    fire_zone = cfg.get("fire_zone") or {}
    fire_bbox = fire_zone.get("bbox")
    if fire_zone.get("enabled") and isinstance(fire_bbox, list) and len(fire_bbox) == 4:
        l, t, r, b = bbox_to_canvas(project, tuple(float(v) for v in fire_bbox))
        draw.rectangle((l, t, r, b), fill="#fee2e266", outline="#dc2626")

    # Roads
    for meta in graph.edge_meta.values():
        geom = meta.geometry or []
        if not geom:
            a = graph.nodes.get(meta.src)
            b = graph.nodes.get(meta.dst)
            if not a or not b:
                continue
            geom = [{"x": a.x, "y": a.y}, {"x": b.x, "y": b.y}]
        pts = [project(float(p["x"]), float(p["y"])) for p in geom]
        if len(pts) >= 2:
            draw.line(pts, fill="#dbe4ee", width=1, joint="curve")

    # Home nodes
    for node_id in homes:
        n = graph.nodes[node_id]
        px, py = project(n.x, n.y)
        draw_diamond(draw, px, py, 7, "#2563eb", "#0f172a")

    # Hide points
    for point_id in points.hide_points:
        n = graph.nodes.get(point_id)
        if not n:
            continue
        px, py = project(n.x, n.y)
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill="#16a34a", outline="#14532d")

    # Launch points
    for point_id in points.launch_points:
        n = graph.nodes.get(point_id)
        if not n:
            continue
        px, py = project(n.x, n.y)
        draw.rectangle((px - 6, py - 6, px + 6, py + 6), fill="#ef4444", outline="#7f1d1d")

    # Depots
    for point_id in points.depots:
        n = graph.nodes.get(point_id)
        if not n:
            continue
        px, py = project(n.x, n.y)
        draw.ellipse((px - 7, py - 7, px + 7, py + 7), fill="#f59e0b", outline="#78350f")

    # Header
    draw.rectangle((12, 12, args.width - 12, 96), fill="#ffffff", outline="#e2e8f0")
    draw.text((24, 22), "Map Elements Overview", fill="#0f172a")
    draw.text(
        (24, 48),
        (
            f"roads={len(graph.edge_meta)}  nodes={len(graph.nodes)}  "
            f"hide={len(points.hide_points)}  launch={len(points.launch_points)}  "
            f"depots={len(points.depots)}  homes={len(homes)}  "
            f"fire_zone={'on' if fire_zone.get('enabled') else 'off'}"
        ),
        fill="#475569",
    )

    # Legend
    legend_y = 72
    draw.text((24, legend_y), "Legend:", fill="#475569")
    x = 88
    draw_diamond(draw, x, legend_y + 8, 6, "#2563eb", "#0f172a")
    draw.text((x + 12, legend_y), "vehicle homes", fill="#0f172a")
    x += 160
    draw.ellipse((x, legend_y + 2, x + 10, legend_y + 12), fill="#16a34a", outline="#14532d")
    draw.text((x + 18, legend_y), "hide points", fill="#0f172a")
    x += 150
    draw.rectangle((x, legend_y + 2, x + 12, legend_y + 14), fill="#ef4444", outline="#7f1d1d")
    draw.text((x + 18, legend_y), "launch points", fill="#0f172a")
    x += 165
    draw.ellipse((x, legend_y + 1, x + 14, legend_y + 15), fill="#f59e0b", outline="#78350f")
    draw.text((x + 22, legend_y), "depots", fill="#0f172a")
    x += 130
    draw.rectangle((x, legend_y + 2, x + 16, legend_y + 14), fill="#fee2e2", outline="#dc2626")
    draw.text((x + 24, legend_y), "fire zone", fill="#0f172a")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    print(f"saved map elements image to {out_path}")


if __name__ == "__main__":
    main()
