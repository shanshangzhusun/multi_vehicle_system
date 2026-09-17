'''
python3 scripts/inspect_candidate_launch_order.py \
  --scheduler-config result/configs/schedulers/scheduler_001.json \
  --received-dir result/received_dian \
  --candidate-count 6 \
  --out-json result/candidate_launch_order_from_received_dian.json

  python3 scripts/build_mock_inputs_from_received_dian.py \
  --received-dir result/received_dian \
  --out-dir result/mock_inputs_from_received \
  --vehicle-start-port 8414 \
  --vehicle-count 64
'''

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvs.scheduler.map_model import MapLoader, SpecialPoints
from mvs.scheduler.scheduler_app import SchedulerApp, VehicleRuntime


class _NullEventLog:
    def log(self, *args: Any, **kwargs: Any) -> None:
        return None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_from_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    obj = _load_json(path)
    data = obj.get("data", obj) if isinstance(obj, dict) else obj
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [dict(data)]
    return []


def _rows_from_received(received_dir: Path, msg_type: str) -> List[Dict[str, Any]]:
    paths = sorted(received_dir.glob(f"*_{msg_type}.json"), key=lambda p: (p.stat().st_mtime, p.name))
    if not paths:
        return []
    if msg_type in {"VEHICLE_DIAN", "VEHICLE_CONTEXT"}:
        by_vehicle: Dict[str, Dict[str, Any]] = {}
        for path in paths:
            obj = _load_json(path)
            rows = obj.get("raw_rows") if isinstance(obj, dict) else None
            for row in rows or []:
                if isinstance(row, dict):
                    key = _row_vehicle_id(row) or path.name.split("_", 1)[0]
                    by_vehicle[key] = dict(row)
        return [by_vehicle[key] for key in sorted(by_vehicle, key=lambda v: int(v) if v.isdigit() else v)]
    def _raw_row_count(path: Path) -> int:
        obj = _load_json(path)
        return len((obj.get("raw_rows") if isinstance(obj, dict) else []) or [])

    best_path = max(paths, key=lambda p: (_raw_row_count(p), p.stat().st_mtime))
    obj = _load_json(best_path)
    rows = obj.get("raw_rows") if isinstance(obj, dict) else None
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _vehicle_context_rows_from_received(received_dir: Path, prefer_vehicle_dian: bool = False) -> List[Dict[str, Any]]:
    if prefer_vehicle_dian:
        rows = _rows_from_received(received_dir, "VEHICLE_DIAN")
        return rows or _rows_from_received(received_dir, "VEHICLE_CONTEXT")
    rows = _rows_from_received(received_dir, "VEHICLE_CONTEXT")
    return rows or _rows_from_received(received_dir, "VEHICLE_DIAN")


def _row_vehicle_id(row: Dict[str, Any]) -> str:
    value = row.get("vehicle_id") or row.get("port") or row.get("vehicle_port")
    return str(int(float(value))) if value not in {None, ""} else ""


def _make_offline_scheduler(config_path: Path, fire_rows: List[dict], hide_rows: List[dict], vehicle_rows: List[dict]) -> SchedulerApp:
    cfg = _load_json(config_path)
    app = SchedulerApp.__new__(SchedulerApp)
    app.node_id = "offline_candidate_inspector"
    app.graph = MapLoader.load_graph_from_config(cfg["map"])
    app.points = SpecialPoints(hide_points=[], launch_points=[], depots=[])
    app.event_log = _NullEventLog()
    app.assignment_scoring_cfg = dict(cfg.get("assignment_scoring", {}))
    app.external_launch_point_rows_by_node = {}
    app.external_hide_point_rows_by_node = {}
    app.external_fa_she_dian = []
    app.external_yin_bi_dian = []
    app.external_vehicle_dian = []
    app._graph_bounds = SchedulerApp._compute_graph_bounds(app)

    app.points.launch_points = SchedulerApp._dian_rows_to_graph_nodes(app, fire_rows, "launch")
    app.points.hide_points = SchedulerApp._dian_rows_to_graph_nodes(app, hide_rows, "hide")

    configured = {str(v["vehicle_id"]): v for v in cfg.get("vehicles", [])}
    vehicle_by_id = {_row_vehicle_id(row): row for row in vehicle_rows if _row_vehicle_id(row)}
    app.vehicles = {}
    for vehicle_id, row in sorted(vehicle_by_id.items()):
        base = configured.get(vehicle_id, {})
        home_node = str(base.get("home_node") or "")
        current_node = SchedulerApp._nearest_graph_node_for_dian(app, row, f"vehicle_{vehicle_id}") or home_node
        app.vehicles[vehicle_id] = VehicleRuntime(
            vehicle_id=vehicle_id,
            endpoint=(str(base.get("host", "")), int(base.get("port", vehicle_id))),
            home_node=home_node or current_node,
            ammo_types=set(base.get("ammo_types", [])),
            speed_mps=float(base.get("speed_mps", 22.222)),
            realtime_scale=float(base.get("realtime_scale", 0.0125)),
            kinematics=dict(base.get("kinematics", {})),
            status="OFFLINE",
            current_node=current_node,
            last_seen="offline",
        )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline inspect scheduler vehicle -> launch candidate ordering.")
    parser.add_argument("--scheduler-config", default="result/configs/scheduler_debug.json")
    parser.add_argument("--dian-dir", default="result/extracted_dian")
    parser.add_argument("--received-dir", default="")
    parser.add_argument("--vehicle-ids", default="", help="Comma-separated vehicle ids to print. Empty prints all.")
    parser.add_argument("--candidate-count", type=int, default=6)
    parser.add_argument("--out-json", default="result/candidate_launch_order_inspect.json")
    parser.add_argument(
        "--prefer-vehicle-dian",
        action="store_true",
        help="Offline inspect only: prefer VEHICLE_DIAN over VEHICLE_CONTEXT as vehicle input.",
    )
    parser.add_argument(
        "--legacy-rank1-no-locality-guard",
        action="store_true",
        help="Inspect old behavior: use Hungarian unique rank1 without locality repair.",
    )
    args = parser.parse_args()

    dian_dir = Path(args.dian_dir)
    received_dir = Path(args.received_dir) if args.received_dir else None
    fire_rows = _rows_from_received(received_dir, "FA_SHE_DIAN") if received_dir else []
    hide_rows = _rows_from_received(received_dir, "YIN_BI_DIAN") if received_dir else []
    vehicle_rows = _vehicle_context_rows_from_received(
        received_dir,
        prefer_vehicle_dian=args.prefer_vehicle_dian,
    ) if received_dir else []
    fire_rows = fire_rows or _rows_from_file(dian_dir / "FA_SHE_DIAN.json") or _rows_from_file(dian_dir / "FA_SHE_DIAN_64.json")
    hide_rows = hide_rows or _rows_from_file(dian_dir / "YIN_BI_DIAN.json") or _rows_from_file(dian_dir / "YIN_BI_DIAN_64.json")
    vehicle_rows = vehicle_rows or _rows_from_file(dian_dir / "VEHICLE_DIAN.json") or _rows_from_file(dian_dir / "VEHICLE_DIAN_16.json")

    app = _make_offline_scheduler(Path(args.scheduler_config), fire_rows, hide_rows, vehicle_rows)
    if args.legacy_rank1_no_locality_guard:
        app._repair_rank1_assignment_locality = lambda assignment, vehicles, ranked_by_vehicle, distance_cache, local_rank_limit: (  # type: ignore[method-assign]
            assignment,
            {"disabled_by_inspect": True},
        )
    all_rows = SchedulerApp._build_vehicle_candidate_launch_points(app, candidate_count=args.candidate_count)
    rows = list(all_rows)
    wanted = {item.strip() for item in args.vehicle_ids.split(",") if item.strip()}
    if wanted:
        rows = [row for row in rows if str(row.get("vehicle_id")) in wanted]

    out_rows = []
    for row in rows:
        vehicle_id = str(row.get("vehicle_id"))
        vehicle = app.vehicles.get(vehicle_id)
        start = app.graph.nodes.get(vehicle.current_node) if vehicle else None
        items = []
        for cand in row.get("candidate_launch_points") or []:
            node_id = str(cand.get("launch_node") or "")
            node = app.graph.nodes.get(node_id)
            dist = None
            if start is not None and node is not None:
                dist = math.hypot(start.x - node.x, start.y - node.y)
            items.append(
                {
                    "rank": cand.get("rank"),
                    "fire_point_id": cand.get("fire_point_id"),
                    "launch_node": node_id,
                    "distance_m": round(dist, 3) if dist is not None else None,
                    "lon": cand.get("lon"),
                    "lat": cand.get("lat"),
                    "hide_candidates": cand.get("candidate_hide_points") or cand.get("hide_candidates") or [],
                }
            )
        out_rows.append(
            {
                "vehicle_id": vehicle_id,
                "current_node": vehicle.current_node if vehicle else None,
                "start_lon": start.lon if start else None,
                "start_lat": start.lat if start else None,
                "candidates": items,
            }
        )

    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(out_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote {args.out_json} total_vehicles={len(all_rows)} printed_vehicles={len(out_rows)} "
        f"launch_points={len(app.points.launch_points)} hide_points={len(app.points.hide_points)}"
    )
    for row in out_rows[: max(20, len(out_rows) if wanted else 20)]:
        summary = ", ".join(
            f"#{c['rank']} {c['fire_point_id']} {c['distance_m']}m" for c in row["candidates"]
        )
        print(f"{row['vehicle_id']} start=({row['start_lon']},{row['start_lat']}) {summary}")


if __name__ == "__main__":
    main()
