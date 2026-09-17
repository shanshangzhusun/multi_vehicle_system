#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.scheduler.map_model import MapLoader


def main() -> None:
    parser = argparse.ArgumentParser(description="Import SHP road/points layers into MVS internal JSON graph")
    parser.add_argument("--roads-shp", required=True, help="road centerline SHP path")
    parser.add_argument("--points-shp", default="", help="optional special points SHP path")
    parser.add_argument("--graph-out", required=True, help="output graph json path")
    parser.add_argument("--points-out", default="", help="optional output points json path")
    parser.add_argument("--lane-width", type=float, default=8.0, help="default single lane width in meters")
    parser.add_argument("--speed-limit", type=float, default=8.0, help="default speed limit in m/s")
    parser.add_argument("--lanes-forward", type=int, default=1)
    parser.add_argument("--lanes-backward", type=int, default=1)
    parser.add_argument("--node-snap-tol", type=float, default=0.5)
    args = parser.parse_args()

    graph = MapLoader.shp_to_graph(
        args.roads_shp,
        default_lane_width=args.lane_width,
        default_speed_limit_mps=args.speed_limit,
        default_lanes_forward=args.lanes_forward,
        default_lanes_backward=args.lanes_backward,
        node_snap_tol=args.node_snap_tol,
    )

    graph_out = Path(args.graph_out)
    graph_out.parent.mkdir(parents=True, exist_ok=True)
    graph_obj = {
        "nodes": [
            {"id": n.node_id, "x": n.x, "y": n.y, "kind": n.kind}
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "id": meta.edge_id,
                "from": meta.src,
                "to": meta.dst,
                "geom_from": meta.geom_from,
                "geom_to": meta.geom_to,
                "cost": meta.cost,
                "width": meta.width,
                "lanes_forward": meta.lanes_forward,
                "lanes_backward": meta.lanes_backward,
                "speed_limit_mps": meta.speed_limit_mps,
                "curvature": meta.curvature,
                "geometry": meta.geometry,
            }
            for meta in graph.edge_meta.values()
        ],
    }
    graph_out.write_text(json.dumps(graph_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.points_shp and args.points_out:
        points = MapLoader.load_points_from_shp(args.points_shp, graph)
        points_obj = {
            "hide_points": points.hide_points,
            "launch_points": points.launch_points,
            "depots": points.depots,
        }
        points_out = Path(args.points_out)
        points_out.parent.mkdir(parents=True, exist_ok=True)
        points_out.write_text(json.dumps(points_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"graph written to {graph_out}")
    if args.points_shp and args.points_out:
        print(f"points written to {args.points_out}")


if __name__ == "__main__":
    main()
